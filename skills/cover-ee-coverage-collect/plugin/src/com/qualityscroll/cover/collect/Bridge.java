package com.qualityscroll.cover.collect;

import java.nio.file.*;
import java.nio.file.attribute.*;
import java.security.MessageDigest;
import java.util.*;
import java.util.concurrent.*;
import java.util.stream.Collectors;
import com.google.gson.*;
import org.eclipse.core.resources.ResourcesPlugin;
import org.eclipse.core.runtime.Platform;
import org.eclipse.core.runtime.Status;
import org.eclipse.debug.core.DebugPlugin;
import org.eclipse.debug.core.ILaunch;
import org.eclipse.cdt.dsf.gdb.launching.GdbLaunch;

/** User-private, local file mailbox. No network listener or arbitrary GDB command. */
final class Bridge implements AutoCloseable {
    static final String ID = "com.qualityscroll.cover.collect";
    private static final Gson JSON = new GsonBuilder().setPrettyPrinting().create();
    private final Path root = ResourcesPlugin.getWorkspace().getRoot().getLocation().toFile().toPath()
        .resolve(".metadata/.plugins/" + ID);
    private final String instance = UUID.randomUUID().toString();
    private final ScheduledExecutorService workers = Executors.newScheduledThreadPool(2, task -> {
        Thread thread = new Thread(task, "COVER CDT collector"); thread.setDaemon(true); return thread;
    });
    private boolean captureBlocked;

    void start() throws Exception {
        Files.createDirectories(root);
        if (Files.isSymbolicLink(root)) throw new IllegalStateException("Bridge directory must not be a link");
        restrictToOwner(root);
        Files.createDirectories(root.resolve("requests"));
        Files.createDirectories(root.resolve("completed"));
        heartbeat();
        workers.scheduleWithFixedDelay(this::heartbeat, 1, 1, TimeUnit.SECONDS);
        workers.scheduleWithFixedDelay(this::poll, 0, 150, TimeUnit.MILLISECONDS);
    }

    private static void restrictToOwner(Path path) throws Exception {
        var posix = Files.getFileAttributeView(path, PosixFileAttributeView.class);
        if (posix != null) { posix.setPermissions(PosixFilePermissions.fromString("rwx------")); return; }
        var acl = Files.getFileAttributeView(path, AclFileAttributeView.class);
        if (acl == null) throw new IllegalStateException("Private bridge permissions unsupported");
        acl.setAcl(List.of(AclEntry.newBuilder().setType(AclEntryType.ALLOW).setPrincipal(Files.getOwner(path))
            .setPermissions(EnumSet.allOf(AclEntryPermission.class))
            .setFlags(AclEntryFlag.DIRECTORY_INHERIT, AclEntryFlag.FILE_INHERIT).build()));
    }

    private void heartbeat() {
        try {
            JsonObject result = new JsonObject();
            result.addProperty("protocol", 1); result.addProperty("instance", instance);
            result.addProperty("javaVersion", System.getProperty("java.version"));
            result.addProperty("workspace", ResourcesPlugin.getWorkspace().getRoot().getLocation().toOSString());
            result.addProperty("ideLocation", Platform.getInstallLocation().getURL().toString());
            result.addProperty("version", Activator.context.getBundle().getVersion().toString());
            result.addProperty("pid", ProcessHandle.current().pid());
            result.addProperty("heartbeatMs", System.currentTimeMillis());
            atomicJson(root.resolve("bridge.json"), result);
        } catch (Exception error) { log(error); }
    }

    private void poll() {
        try (var directories = Files.newDirectoryStream(root.resolve("requests"))) {
            for (Path folder : directories) {
                if (!folder.getFileName().toString().matches("[0-9a-f]{32}") || Files.isSymbolicLink(folder)) continue;
                if (Files.isRegularFile(folder.resolve("ack.json"), LinkOption.NOFOLLOW_LINKS)
                        && Files.isRegularFile(folder.resolve("response.json"), LinkOption.NOFOLLOW_LINKS)) {
                    // Only acknowledged requests are archived; raw data already has a durable client copy.
                    Files.deleteIfExists(folder.resolve("memory.bin"));
                    Files.move(folder, root.resolve("completed").resolve(folder.getFileName()), StandardCopyOption.ATOMIC_MOVE);
                    continue;
                }
                Path requestFile = folder.resolve("request.json");
                if (!Files.isRegularFile(requestFile, LinkOption.NOFOLLOW_LINKS)
                    || Files.exists(folder.resolve("working.json")) || Files.exists(folder.resolve("response.json"))) continue;
                if (Files.size(requestFile) > 16384) continue;
                Files.move(requestFile, folder.resolve("working.json"), StandardCopyOption.ATOMIC_MOVE);
                process(folder);
            }
        } catch (Exception error) { log(error); }
    }

    private void process(Path folder) throws Exception {
        JsonObject response = new JsonObject();
        response.addProperty("requestId", folder.getFileName().toString());
        response.addProperty("instance", instance);
        try {
            JsonObject request = new JsonParser().parse(Files.readString(folder.resolve("working.json"))).getAsJsonObject();
            if (!instance.equals(request.get("instance").getAsString())) throw new IllegalStateException("IDE_INSTANCE_CHANGED");
            long created = request.get("createdMs").getAsLong();
            long now = System.currentTimeMillis();
            if (now-created > 60000 || created-now > 5000) throw new IllegalStateException("REQUEST_EXPIRED");
            String action = request.get("action").getAsString();
            if (action.equals("sessions")) response.add("sessions", sessions());
            else if (action.equals("capture")) {
                if (captureBlocked) throw new IllegalStateException("CAPTURE_BLOCKED_AFTER_TIMEOUT: inspect IDE and restart plugin");
                int seconds = request.has("captureTimeoutSeconds") ? request.get("captureTimeoutSeconds").getAsInt() : 60;
                if (seconds < 10 || seconds > 600) throw new IllegalStateException("INVALID_CAPTURE_TIMEOUT");
                capture(request, response, folder, now+seconds*1000L);
            } else throw new IllegalStateException("UNKNOWN_ACTION");
            response.addProperty("ok", true);
        } catch (Exception error) {
            String message = error.toString();
            for (Throwable cause=error.getCause(); cause!=null; cause=cause.getCause()) message += "; " + cause;
            if (message.toLowerCase(Locale.ROOT).contains("timeout")) captureBlocked = true;
            response.addProperty("ok", false); response.addProperty("error", message);
        }
        atomicJson(folder.resolve("response.json"), response);
    }

    private List<GdbLaunch> launches() {
        List<GdbLaunch> result = new ArrayList<>();
        for (ILaunch launch : DebugPlugin.getDefault().getLaunchManager().getLaunches()) {
            if (!(launch instanceof GdbLaunch)) continue;
            GdbLaunch gdb = (GdbLaunch) launch;
            if (!gdb.isTerminated() && gdb.getSession()!=null && gdb.getSession().isActive()) result.add(gdb);
        }
        return result;
    }

    private JsonArray sessions() throws Exception {
        JsonArray result = new JsonArray();
        for (GdbLaunch launch : launches()) {
            JsonObject item = new JsonObject();
            item.addProperty("sessionId", launch.getSession().getId());
            item.addProperty("name", launch.getLaunchConfiguration().getName());
            String project = launch.getLaunchConfiguration().getAttribute("org.eclipse.cdt.launch.PROJECT_ATTR", "");
            item.addProperty("projectName", project);
            if (!project.isEmpty()) {
                var resource = ResourcesPlugin.getWorkspace().getRoot().getProject(project);
                if (resource.exists() && resource.getLocation()!=null) item.addProperty("projectPath", resource.getLocation().toOSString());
            }
            try (DsfCapture capture = new DsfCapture(launch, System.currentTimeMillis()+10000)) {
                item.addProperty("elf", capture.program().toString());
                try {
                    capture.inspect();
                    item.addProperty("captureSupported", true);
                } catch (Exception error) {
                    item.addProperty("captureSupported", false);
                    item.addProperty("reason", error.toString());
                }
            } catch (Exception error) { item.addProperty("unavailable", error.toString()); }
            result.add(item);
        }
        return result;
    }

    private void capture(JsonObject request, JsonObject response, Path folder, long deadline) throws Exception {
        String sessionId = request.get("sessionId").getAsString();
        var candidates = launches().stream().filter(g -> g.getSession().getId().equals(sessionId)).collect(Collectors.toList());
        if (candidates.size()!=1) throw new IllegalStateException("SESSION_NOT_FOUND");
        Path expected = Path.of(request.get("elf").getAsString()).toRealPath();
        String expectedHash = request.get("elfSha256").getAsString();
        try (DsfCapture capture = new DsfCapture(candidates.get(0), deadline)) {
            Path program = capture.program();
            if (!Files.isSameFile(expected, program) || !hash(Files.readAllBytes(program)).equals(expectedHash))
                throw new IllegalStateException("ELF_MISMATCH");
            var result = capture.capture((done, total) -> {
                JsonObject progress = new JsonObject();
                progress.addProperty("bytesRead", done); progress.addProperty("totalBytes", total);
                try { atomicJson(folder.resolve("progress.json"), progress); }
                catch (Exception error) { throw new IllegalStateException("PROGRESS_WRITE_FAILED", error); }
            });
            if (!Files.isSameFile(expected, capture.program()) || !hash(Files.readAllBytes(program)).equals(expectedHash))
                throw new IllegalStateException("ELF_CHANGED_DURING_CAPTURE");
            Files.write(folder.resolve("memory.bin"), result.bytes(), StandardOpenOption.CREATE_NEW);
            response.addProperty("sessionId", sessionId); response.addProperty("elf", program.toString());
            response.addProperty("elfSha256", expectedHash);
            response.addProperty("bytes", result.bytes().length); response.addProperty("sha256", hash(result.bytes()));
            response.addProperty("symbol", "sdump_buf"); response.addProperty("arrayType", result.type());
            response.addProperty("address", result.address()); response.addProperty("targetStopped", true);
        }
    }

    static String hash(byte[] bytes) throws Exception {
        StringBuilder result = new StringBuilder(64);
        for (byte value : MessageDigest.getInstance("SHA-256").digest(bytes)) {
            result.append(Character.forDigit((value & 255) >>> 4, 16));
            result.append(Character.forDigit(value & 15, 16));
        }
        return result.toString();
    }

    private static void atomicJson(Path destination, JsonObject content) throws Exception {
        Path temporary = destination.resolveSibling(destination.getFileName()+".tmp");
        Files.writeString(temporary, JSON.toJson(content));
        Files.move(temporary, destination, StandardCopyOption.ATOMIC_MOVE, StandardCopyOption.REPLACE_EXISTING);
    }

    private static void log(Exception error) {
        Platform.getLog(Activator.context.getBundle()).log(new Status(Status.ERROR, ID, "COVER collector bridge error", error));
    }

    public void close() throws Exception {
        workers.shutdownNow();
        Files.deleteIfExists(root.resolve("bridge.json"));
    }
}
