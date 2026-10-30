package com.qualityscroll.cover.collect;

import org.osgi.framework.BundleActivator;
import org.osgi.framework.BundleContext;

public final class Activator implements BundleActivator {
    static BundleContext context;
    private static Bridge bridge;

    public void start(BundleContext value) { context = value; }

    static synchronized void startBridge() throws Exception {
        if (bridge == null) {
            Bridge candidate = new Bridge();
            candidate.start();
            bridge = candidate;
        }
    }

    public void stop(BundleContext value) throws Exception {
        if (bridge != null) bridge.close();
        bridge = null;
        context = null;
    }
}
