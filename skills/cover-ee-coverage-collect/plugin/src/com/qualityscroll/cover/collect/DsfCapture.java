package com.qualityscroll.cover.collect;

import java.io.ByteArrayOutputStream;
import java.nio.file.Path;
import java.util.concurrent.TimeUnit;
import java.util.function.Consumer;
import java.util.concurrent.Callable;
import org.eclipse.cdt.dsf.concurrent.*;
import org.eclipse.cdt.dsf.datamodel.*;
import org.eclipse.cdt.dsf.debug.service.*;
import org.eclipse.cdt.dsf.debug.service.command.ICommandControlService;
import org.eclipse.cdt.dsf.gdb.launching.GdbLaunch;
import org.eclipse.cdt.dsf.gdb.service.IGDBBackend;
import org.eclipse.cdt.dsf.service.*;
import org.eclipse.core.runtime.Status;
import org.eclipse.debug.core.model.MemoryByte;

/** All DSF calls run on its executor. Only the bridge worker waits for results. */
final class DsfCapture implements AutoCloseable {
    private final DsfSession session;
    private final DsfServicesTracker services;
    private final long deadline;
    private final ResumeWatch watch = new ResumeWatch();

    public static final class ResumeWatch {
        volatile boolean resumed;
        @DsfServiceEventHandler
        public void eventDispatched(IRunControl.IResumedDMEvent event) { resumed = true; }
    }

    DsfCapture(GdbLaunch launch, long deadline) throws Exception {
        session = launch.getSession();
        this.deadline = deadline;
        services = sync(() -> new DsfServicesTracker(Activator.context, session.getId()));
        sync(() -> { session.addServiceEventListener(watch, null); return null; });
    }

    private <T> T request(Consumer<DataRequestMonitor<T>> action) throws Exception {
        if (System.currentTimeMillis() >= deadline) throw new IllegalStateException("CAPTURE_TIMEOUT");
        Query<T> query = new Query<>() {
            protected void execute(DataRequestMonitor<T> monitor) {
                try { action.accept(monitor); }
                catch (Exception error) { monitor.setStatus(new Status(Status.ERROR, Bridge.ID, error.toString(), error)); monitor.done(); }
            }
        };
        session.getExecutor().execute(query);
        try { return query.get(Math.max(1, Math.min(10000, deadline-System.currentTimeMillis())), TimeUnit.MILLISECONDS); }
        catch (java.util.concurrent.TimeoutException error) {
            query.cancel(false);
            throw new IllegalStateException("DSF_TIMEOUT: inspect IDE before another request", error);
        }
    }

    private <T> T sync(Callable<T> action) throws Exception {
        return request(monitor -> {
            try { monitor.setData(action.call()); }
            catch (Exception error) { monitor.setStatus(new Status(Status.ERROR, Bridge.ID, error.toString(), error)); }
            monitor.done();
        });
    }

    private <T> T service(Class<T> type) {
        T value = services.getService(type);
        if (value == null) throw new IllegalStateException("DSF_SERVICE_MISSING: " + type.getSimpleName());
        return value;
    }

    Path program() throws Exception {
        return sync(() -> service(IGDBBackend.class).getProgramPath().toFile().toPath().toRealPath());
    }

    static final class Result {
        private final byte[] bytes;
        private final String address, type;
        Result(byte[] bytes, String address, String type) { this.bytes = bytes; this.address = address; this.type = type; }
        byte[] bytes() { return bytes; }
        String address() { return address; }
        String type() { return type; }
    }

    static final class Target {
        private final IRunControl.IContainerDMContext container;
        private final IRunControl.IExecutionDMContext thread;
        Target(IRunControl.IContainerDMContext container, IRunControl.IExecutionDMContext thread) { this.container = container; this.thread = thread; }
        IRunControl.IContainerDMContext container() { return container; }
        IRunControl.IExecutionDMContext thread() { return thread; }
    }

    Target inspect() throws Exception {
        IDMContext[] processes = request(m -> service(IProcesses.class).getProcessesBeingDebugged(
            service(ICommandControlService.class).getContext(), m));
        if (processes.length != 1) throw new IllegalStateException("AMBIGUOUS_TARGET: select a single-core debug launch; multi-core capture is not validated");
        var container = DMContexts.getAncestorOfType(processes[0], IRunControl.IContainerDMContext.class);
        if (container == null) throw new IllegalStateException("TARGET_CONTEXT_MISSING");
        IRunControl.IExecutionDMContext[] threads = request(m -> service(IRunControl.class).getExecutionContexts(container, m));
        if (threads.length != 1) throw new IllegalStateException("AMBIGUOUS_THREAD: RTOS/multiple execution contexts require target-specific validation");
        return new Target(container, threads[0]);
    }

    private IExpressions.IExpressionDMContext bufferExpression(IRunControl.IExecutionDMContext thread) throws Exception {
        var global = sync(() -> service(IExpressions.class).createExpression(thread, "::sdump_buf"));
        try {
            IExpressions.IExpressionDMData data = this.<IExpressions.IExpressionDMData>request(
                m -> service(IExpressions.class).getExpressionData(global, m));
            // Some CDT/GDB combinations resolve the global expression without
            // exposing its DWARF aggregate type in the execution context.  A
            // frame-scoped expression can expose the same global as an array.
            // Only use that result if the later byte-array checks accept it;
            // pointers and unknown types still fail closed.
            if (data.getBasicType() == IExpressions.IExpressionDMData.BasicType.array) return global;
        } catch (Exception error) {
            // Never retry a timed-out request that may still be active in GDB.
            for (Throwable cause=error; cause!=null; cause=cause.getCause()) {
                if (cause.toString().toLowerCase().contains("timeout")) throw error;
            }
        }
        var frame = this.<IStack.IFrameDMContext>request(m -> service(IStack.class).getTopFrame(thread, m));
        return sync(() -> service(IExpressions.class).createExpression(frame, "::sdump_buf"));
    }

    Result capture(java.util.function.BiConsumer<Integer, Integer> progress) throws Exception {
        // Unsupported topologies must be rejected before any suspension request.
        Target target = inspect();
        var container = target.container();
        boolean stopped = sync(() -> service(IRunControl.class).isSuspended(container));
        if (!stopped) {
            this.<Void>request(m -> service(IRunControl.class).suspend(container, m));
            while (!sync(() -> service(IRunControl.class).isSuspended(container))) {
                if (System.currentTimeMillis() >= deadline) throw new IllegalStateException("SUSPEND_TIMEOUT");
                Thread.sleep(50);
            }
        }
        // Start the consistency window only after suspension completes.
        sync(() -> { watch.resumed = false; return null; });
        var thread = inspect().thread();
        var expression = bufferExpression(thread);
        IExpressions.IExpressionDMData data = request(m -> service(IExpressions.class).getExpressionData(expression, m));
        String type = data.getTypeName();
        if (data.getBasicType() != IExpressions.IExpressionDMData.BasicType.array) {
            throw new IllegalStateException("BUFFER_NOT_BYTE_ARRAY: " + type);
        }
        IExpressions.IExpressionDMAddress location = request(m -> service(IExpressions.class).getExpressionAddressData(expression, m));
        if (location.getMemorySpaceID() != null && !location.getMemorySpaceID().isEmpty())
            throw new IllegalStateException("MEMORY_SPACE_UNSUPPORTED");
        int size = location.getSize();
        if (size <= 0 || size > 64*1024*1024) throw new IllegalStateException("BUFFER_SIZE_UNSUPPORTED: " + size);
        BufferRules.validateArray(true, size);
        var memoryContext = DMContexts.getAncestorOfType(thread, IMemory.IMemoryDMContext.class);
        if (memoryContext == null) throw new IllegalStateException("MEMORY_CONTEXT_MISSING");
        // DSF memory can cache an earlier Memory View read; flush it before capture.
        sync(() -> {
            IMemory memory = service(IMemory.class);
            if (memory instanceof ICachingService) ((ICachingService) memory).flushCache(memoryContext);
            return null;
        });
        ByteArrayOutputStream output = new ByteArrayOutputStream(size);
        for (int offset=0; offset<size;) {
            ensureStopped(thread);
            int start=offset, chunkSize=Math.min(4096, size-offset);
            MemoryByte[] chunk = request(m -> service(IMemory.class).getMemory(memoryContext,
                location.getAddress(), start, 1, chunkSize, m));
            if (chunk.length != chunkSize) throw new IllegalStateException("SHORT_MEMORY_READ");
            for (MemoryByte value : chunk) {
                if (!value.isReadable()) throw new IllegalStateException("UNREADABLE_MEMORY");
                output.write(value.getValue());
            }
            offset += chunkSize;
            progress.accept(offset, size);
        }
        ensureStopped(thread);
        return new Result(output.toByteArray(), location.getAddress().toHexAddressString(), type);
    }

    private void ensureStopped(IRunControl.IExecutionDMContext thread) throws Exception {
        if (!sync(() -> !watch.resumed && service(IRunControl.class).isSuspended(thread)))
            throw new IllegalStateException("TARGET_RESUMED_DURING_CAPTURE");
    }

    public void close() {
        // Queue cleanup even if a debugger callback timed out; never resume the target.
        if (session.isActive()) session.getExecutor().execute(() -> {
            session.removeServiceEventListener(watch);
            services.dispose();
        });
    }
}
