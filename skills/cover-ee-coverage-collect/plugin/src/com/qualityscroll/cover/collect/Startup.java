package com.qualityscroll.cover.collect;

import org.eclipse.ui.IStartup;
import org.eclipse.core.runtime.Platform;
import org.eclipse.core.runtime.Status;

public final class Startup implements IStartup {
    public void earlyStartup() {
        try { Activator.startBridge(); }
        catch (Exception error) {
            Platform.getLog(Activator.context.getBundle()).log(new Status(Status.ERROR, Bridge.ID, "COVER collector startup failed", error));
        }
    }
}
