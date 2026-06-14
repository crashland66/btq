import Foundation
import SQLite3

public enum SQLiteFieldCaptureStoreError: Error, Equatable {
    case openFailed(String)
    case prepareFailed(String)
    case stepFailed(String)
    case bindFailed(String)
    case missingBlob
}

public actor SQLiteFieldCaptureStore: FieldCaptureStore {
    private static let snapshotKey = "field_capture_snapshot_v1"
    private let fileURL: URL
    private let encoder: JSONEncoder
    private let decoder: JSONDecoder

    public init(fileURL: URL = SQLiteFieldCaptureStore.defaultFileURL()) {
        self.fileURL = fileURL
        encoder = JSONEncoder()
        encoder.dateEncodingStrategy = .iso8601
        encoder.outputFormatting = [.sortedKeys]
        decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .iso8601
    }

    public func load() async throws -> FieldCaptureSnapshot {
        try withDatabase { database in
            try migrate(database)
            let data = try readSnapshot(database)
            guard let data else {
                return FieldCaptureSnapshot(account: .defaultProduction)
            }
            return try decoder.decode(FieldCaptureSnapshot.self, from: data)
        }
    }

    public func save(_ snapshot: FieldCaptureSnapshot) async throws {
        let data = try encoder.encode(snapshot)
        try withDatabase { database in
            try migrate(database)
            try writeSnapshot(data, database)
        }
    }

    public static func defaultFileURL() -> URL {
        let base = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first
            ?? FileManager.default.temporaryDirectory
        return base
            .appendingPathComponent("BTQFieldCapture", isDirectory: true)
            .appendingPathComponent("field_capture.sqlite3")
    }

    private func withDatabase<T>(_ operation: (OpaquePointer) throws -> T) throws -> T {
        try FileManager.default.createDirectory(at: fileURL.deletingLastPathComponent(), withIntermediateDirectories: true)
        var database: OpaquePointer?
        let flags = SQLITE_OPEN_CREATE | SQLITE_OPEN_READWRITE | SQLITE_OPEN_FULLMUTEX
        guard sqlite3_open_v2(fileURL.path, &database, flags, nil) == SQLITE_OK, let database else {
            let message = database.map { String(cString: sqlite3_errmsg($0)) } ?? "unknown"
            if let database { sqlite3_close(database) }
            throw SQLiteFieldCaptureStoreError.openFailed(message)
        }
        defer { sqlite3_close(database) }
        return try operation(database)
    }

    private func migrate(_ database: OpaquePointer) throws {
        try execute(
            """
            PRAGMA journal_mode=WAL;
            PRAGMA foreign_keys=ON;
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY NOT NULL,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS snapshots (
                key TEXT PRIMARY KEY NOT NULL,
                payload BLOB NOT NULL,
                updated_at TEXT NOT NULL
            );
            INSERT INTO metadata(key, value)
            VALUES ('schema_version', '1')
            ON CONFLICT(key) DO UPDATE SET value = excluded.value;
            """,
            database
        )
    }

    private func readSnapshot(_ database: OpaquePointer) throws -> Data? {
        let sql = "SELECT payload FROM snapshots WHERE key = ? LIMIT 1;"
        var statement: OpaquePointer?
        guard sqlite3_prepare_v2(database, sql, -1, &statement, nil) == SQLITE_OK, let statement else {
            throw SQLiteFieldCaptureStoreError.prepareFailed(errorMessage(database))
        }
        defer { sqlite3_finalize(statement) }

        guard sqlite3_bind_text(statement, 1, Self.snapshotKey, -1, transientDestructor) == SQLITE_OK else {
            throw SQLiteFieldCaptureStoreError.bindFailed(errorMessage(database))
        }

        let result = sqlite3_step(statement)
        if result == SQLITE_DONE {
            return nil
        }
        guard result == SQLITE_ROW else {
            throw SQLiteFieldCaptureStoreError.stepFailed(errorMessage(database))
        }
        guard let bytes = sqlite3_column_blob(statement, 0) else {
            throw SQLiteFieldCaptureStoreError.missingBlob
        }
        let count = Int(sqlite3_column_bytes(statement, 0))
        return Data(bytes: bytes, count: count)
    }

    private func writeSnapshot(_ data: Data, _ database: OpaquePointer) throws {
        let sql = """
        INSERT INTO snapshots(key, payload, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET payload = excluded.payload, updated_at = excluded.updated_at;
        """
        var statement: OpaquePointer?
        guard sqlite3_prepare_v2(database, sql, -1, &statement, nil) == SQLITE_OK, let statement else {
            throw SQLiteFieldCaptureStoreError.prepareFailed(errorMessage(database))
        }
        defer { sqlite3_finalize(statement) }

        guard sqlite3_bind_text(statement, 1, Self.snapshotKey, -1, transientDestructor) == SQLITE_OK else {
            throw SQLiteFieldCaptureStoreError.bindFailed(errorMessage(database))
        }
        let bindBlobResult = data.withUnsafeBytes { rawBuffer in
            sqlite3_bind_blob(statement, 2, rawBuffer.baseAddress, Int32(data.count), transientDestructor)
        }
        guard bindBlobResult == SQLITE_OK else {
            throw SQLiteFieldCaptureStoreError.bindFailed(errorMessage(database))
        }
        guard sqlite3_bind_text(statement, 3, BTQFormatting.fieldTimestamp(), -1, transientDestructor) == SQLITE_OK else {
            throw SQLiteFieldCaptureStoreError.bindFailed(errorMessage(database))
        }

        guard sqlite3_step(statement) == SQLITE_DONE else {
            throw SQLiteFieldCaptureStoreError.stepFailed(errorMessage(database))
        }
    }

    private func execute(_ sql: String, _ database: OpaquePointer) throws {
        var error: UnsafeMutablePointer<CChar>?
        let result = sqlite3_exec(database, sql, nil, nil, &error)
        if result != SQLITE_OK {
            let message = error.map { String(cString: $0) } ?? errorMessage(database)
            sqlite3_free(error)
            throw SQLiteFieldCaptureStoreError.stepFailed(message)
        }
    }

    private func errorMessage(_ database: OpaquePointer) -> String {
        String(cString: sqlite3_errmsg(database))
    }
}

private let transientDestructor = unsafeBitCast(-1, to: sqlite3_destructor_type.self)
