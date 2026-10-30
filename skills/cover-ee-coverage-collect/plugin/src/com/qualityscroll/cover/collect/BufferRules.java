package com.qualityscroll.cover.collect;

/** Pure checks for the debugger-reported array metadata. */
final class BufferRules {
    static void validateArray(boolean array, int totalBytes) {
        // CDT may expose an array child as a debugger UI chunk (for example
        // "unsigned char [100]") rather than as one scalar byte. The parent
        // DWARF array metadata and its memory size are the authoritative checks;
        // child typeId/typeName/count are not byte-element metadata.
        if (!array)
            throw new IllegalStateException("BUFFER_NOT_BYTE_ARRAY");
        if (totalBytes <= 0 || totalBytes > 64*1024*1024)
            throw new IllegalStateException("BUFFER_SIZE_UNSUPPORTED: " + totalBytes);
    }
}
