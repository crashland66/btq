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
            let snapshot = try data.map { try decoder.decode(FieldCaptureSnapshot.self, from: $0) }
                ?? FieldCaptureSnapshot(account: .defaultProduction)
            return try applyingDraftPhotoJournal(to: snapshot, database: database)
        }
    }

    public func save(_ snapshot: FieldCaptureSnapshot) async throws {
        let data = try encoder.encode(snapshot)
        try withDatabase { database in
            try migrate(database)
            try inTransaction(database) {
                try writeSnapshot(data, database)
                try execute("DELETE FROM draft_photos; DELETE FROM draft_captures;", database)
            }
        }
    }

    public func appendDraftPhoto(
        _ photo: CapturePhoto,
        to draft: LocalCapture,
        accountID: UUID,
        snapshot _: FieldCaptureSnapshot
    ) async throws {
        var metadata = draft
        metadata.photos = []
        let metadataData = try encoder.encode(metadata)
        let photoData = try encoder.encode(photo)
        let position = draft.photos.firstIndex(where: { $0.id == photo.id }) ?? max(0, draft.photos.count - 1)

        try withDatabase { database in
            try migrate(database)
            try inTransaction(database) {
                try upsertDraftMetadata(
                    metadataData,
                    accountID: accountID,
                    captureID: draft.captureID,
                    database: database
                )
                try insertDraftPhoto(
                    photoData,
                    photoID: photo.id,
                    position: position,
                    accountID: accountID,
                    captureID: draft.captureID,
                    database: database
                )
            }
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
        try LocalFilePrivacy.prepareDirectory(fileURL.deletingLastPathComponent())
        var database: OpaquePointer?
        let flags = SQLITE_OPEN_CREATE | SQLITE_OPEN_READWRITE | SQLITE_OPEN_FULLMUTEX
        guard sqlite3_open_v2(fileURL.path, &database, flags, nil) == SQLITE_OK, let database else {
            let message = database.map { String(cString: sqlite3_errmsg($0)) } ?? "unknown"
            if let database { sqlite3_close(database) }
            throw SQLiteFieldCaptureStoreError.openFailed(message)
        }
        defer {
            sqlite3_close(database)
            try? protectSQLiteFiles()
        }
        let result = try operation(database)
        try protectSQLiteFiles()
        return result
    }

    private func migrate(_ database: OpaquePointer) throws {
        try execute(
            """
            PRAGMA busy_timeout=5000;
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=FULL;
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
            CREATE TABLE IF NOT EXISTS draft_captures (
                account_id TEXT NOT NULL,
                capture_id TEXT NOT NULL,
                payload BLOB NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(account_id, capture_id)
            );
            CREATE TABLE IF NOT EXISTS draft_photos (
                account_id TEXT NOT NULL,
                capture_id TEXT NOT NULL,
                photo_id TEXT NOT NULL,
                position INTEGER NOT NULL,
                payload BLOB NOT NULL,
                PRIMARY KEY(account_id, capture_id, photo_id),
                FOREIGN KEY(account_id, capture_id)
                    REFERENCES draft_captures(account_id, capture_id)
                    ON DELETE CASCADE
            );
            INSERT INTO metadata(key, value)
            VALUES ('schema_version', '2')
            ON CONFLICT(key) DO UPDATE SET value = excluded.value;
            """,
            database
        )
    }

    private func applyingDraftPhotoJournal(
        to snapshot: FieldCaptureSnapshot,
        database: OpaquePointer
    ) throws -> FieldCaptureSnapshot {
        var merged = snapshot
        for storedDraft in try readDrafts(database) {
            let journalPhotos = try readDraftPhotos(
                accountID: storedDraft.accountID,
                captureID: storedDraft.capture.captureID,
                database: database
            )
            merge(
                draft: storedDraft.capture,
                journalPhotos: journalPhotos,
                accountID: storedDraft.accountID,
                into: &merged
            )
        }
        return merged
    }

    private func merge(
        draft: LocalCapture,
        journalPhotos: [StoredDraftPhoto],
        accountID: UUID,
        into snapshot: inout FieldCaptureSnapshot
    ) {
        if snapshot.account.id == accountID {
            apply(draft: draft, journalPhotos: journalPhotos, to: &snapshot.captures)
        }
        if let workspaceIndex = snapshot.accountWorkspaces.firstIndex(where: { $0.account.id == accountID }) {
            apply(
                draft: draft,
                journalPhotos: journalPhotos,
                to: &snapshot.accountWorkspaces[workspaceIndex].captures
            )
        }
    }

    /// Journal rows are a delta since the most recent full save. Preserve every photo
    /// in that authoritative snapshot, then insert or update each journaled photo at its
    /// absolute shot position. This makes every save/append interleaving converge on the
    /// complete operator-visible photo list without re-journaling the full snapshot.
    private func apply(
        draft: LocalCapture,
        journalPhotos: [StoredDraftPhoto],
        to captures: inout [LocalCapture]
    ) {
        var mergedDraft = draft
        if let index = captures.firstIndex(where: { $0.captureID == draft.captureID }) {
            mergedDraft.photos = merging(
                snapshotPhotos: captures[index].photos,
                journalPhotos: journalPhotos
            )
            captures[index] = mergedDraft
        } else {
            mergedDraft.photos = merging(snapshotPhotos: [], journalPhotos: journalPhotos)
            captures.append(mergedDraft)
        }
    }

    private func merging(
        snapshotPhotos: [CapturePhoto],
        journalPhotos: [StoredDraftPhoto]
    ) -> [CapturePhoto] {
        var photos = snapshotPhotos
        for storedPhoto in journalPhotos {
            photos.removeAll { $0.id == storedPhoto.photo.id }
            photos.insert(storedPhoto.photo, at: min(storedPhoto.position, photos.count))
        }
        return photos
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

    private func readDrafts(_ database: OpaquePointer) throws -> [StoredDraft] {
        let sql = "SELECT account_id, payload FROM draft_captures ORDER BY updated_at, capture_id;"
        var statement: OpaquePointer?
        guard sqlite3_prepare_v2(database, sql, -1, &statement, nil) == SQLITE_OK, let statement else {
            throw SQLiteFieldCaptureStoreError.prepareFailed(errorMessage(database))
        }
        defer { sqlite3_finalize(statement) }

        var drafts: [StoredDraft] = []
        while true {
            let result = sqlite3_step(statement)
            if result == SQLITE_DONE {
                return drafts
            }
            guard result == SQLITE_ROW,
                  let accountText = sqlite3_column_text(statement, 0),
                  let accountID = UUID(uuidString: String(cString: accountText)),
                  let payload = data(from: statement, column: 1) else {
                throw SQLiteFieldCaptureStoreError.missingBlob
            }
            drafts.append(
                StoredDraft(
                    accountID: accountID,
                    capture: try decoder.decode(LocalCapture.self, from: payload)
                )
            )
        }
    }

    private func readDraftPhotos(
        accountID: UUID,
        captureID: String,
        database: OpaquePointer
    ) throws -> [StoredDraftPhoto] {
        let sql = """
        SELECT position, payload FROM draft_photos
        WHERE account_id = ? AND capture_id = ?
        ORDER BY position, rowid;
        """
        var statement: OpaquePointer?
        guard sqlite3_prepare_v2(database, sql, -1, &statement, nil) == SQLITE_OK, let statement else {
            throw SQLiteFieldCaptureStoreError.prepareFailed(errorMessage(database))
        }
        defer { sqlite3_finalize(statement) }

        try bindText(accountID.uuidString, at: 1, to: statement, database: database)
        try bindText(captureID, at: 2, to: statement, database: database)

        var photos: [StoredDraftPhoto] = []
        while true {
            let result = sqlite3_step(statement)
            if result == SQLITE_DONE {
                return photos
            }
            guard result == SQLITE_ROW,
                  sqlite3_column_type(statement, 0) == SQLITE_INTEGER,
                  let payload = data(from: statement, column: 1) else {
                throw SQLiteFieldCaptureStoreError.missingBlob
            }
            photos.append(
                StoredDraftPhoto(
                    position: max(0, Int(sqlite3_column_int64(statement, 0))),
                    photo: try decoder.decode(CapturePhoto.self, from: payload)
                )
            )
        }
    }

    private func upsertDraftMetadata(
        _ data: Data,
        accountID: UUID,
        captureID: String,
        database: OpaquePointer
    ) throws {
        let sql = """
        INSERT INTO draft_captures(account_id, capture_id, payload, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(account_id, capture_id) DO UPDATE SET
            payload = excluded.payload,
            updated_at = excluded.updated_at;
        """
        var statement: OpaquePointer?
        guard sqlite3_prepare_v2(database, sql, -1, &statement, nil) == SQLITE_OK, let statement else {
            throw SQLiteFieldCaptureStoreError.prepareFailed(errorMessage(database))
        }
        defer { sqlite3_finalize(statement) }

        try bindText(accountID.uuidString, at: 1, to: statement, database: database)
        try bindText(captureID, at: 2, to: statement, database: database)
        try bindBlob(data, at: 3, to: statement, database: database)
        try bindText(BTQFormatting.fieldTimestamp(), at: 4, to: statement, database: database)
        guard sqlite3_step(statement) == SQLITE_DONE else {
            throw SQLiteFieldCaptureStoreError.stepFailed(errorMessage(database))
        }
    }

    private func insertDraftPhoto(
        _ data: Data,
        photoID: UUID,
        position: Int,
        accountID: UUID,
        captureID: String,
        database: OpaquePointer
    ) throws {
        let sql = """
        INSERT INTO draft_photos(account_id, capture_id, photo_id, position, payload)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(account_id, capture_id, photo_id) DO UPDATE SET
            position = excluded.position,
            payload = excluded.payload;
        """
        var statement: OpaquePointer?
        guard sqlite3_prepare_v2(database, sql, -1, &statement, nil) == SQLITE_OK, let statement else {
            throw SQLiteFieldCaptureStoreError.prepareFailed(errorMessage(database))
        }
        defer { sqlite3_finalize(statement) }

        try bindText(accountID.uuidString, at: 1, to: statement, database: database)
        try bindText(captureID, at: 2, to: statement, database: database)
        try bindText(photoID.uuidString, at: 3, to: statement, database: database)
        guard sqlite3_bind_int64(statement, 4, sqlite3_int64(position)) == SQLITE_OK else {
            throw SQLiteFieldCaptureStoreError.bindFailed(errorMessage(database))
        }
        try bindBlob(data, at: 5, to: statement, database: database)
        guard sqlite3_step(statement) == SQLITE_DONE else {
            throw SQLiteFieldCaptureStoreError.stepFailed(errorMessage(database))
        }
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

    private func inTransaction<T>(_ database: OpaquePointer, operation: () throws -> T) throws -> T {
        try execute("BEGIN IMMEDIATE;", database)
        do {
            let value = try operation()
            try execute("COMMIT;", database)
            return value
        } catch {
            try? execute("ROLLBACK;", database)
            throw error
        }
    }

    private func bindText(
        _ value: String,
        at index: Int32,
        to statement: OpaquePointer,
        database: OpaquePointer
    ) throws {
        guard sqlite3_bind_text(statement, index, value, -1, transientDestructor) == SQLITE_OK else {
            throw SQLiteFieldCaptureStoreError.bindFailed(errorMessage(database))
        }
    }

    private func bindBlob(
        _ data: Data,
        at index: Int32,
        to statement: OpaquePointer,
        database: OpaquePointer
    ) throws {
        let result = data.withUnsafeBytes { rawBuffer in
            sqlite3_bind_blob(statement, index, rawBuffer.baseAddress, Int32(data.count), transientDestructor)
        }
        guard result == SQLITE_OK else {
            throw SQLiteFieldCaptureStoreError.bindFailed(errorMessage(database))
        }
    }

    private func data(from statement: OpaquePointer, column: Int32) -> Data? {
        guard let bytes = sqlite3_column_blob(statement, column) else { return nil }
        return Data(bytes: bytes, count: Int(sqlite3_column_bytes(statement, column)))
    }

    private func errorMessage(_ database: OpaquePointer) -> String {
        String(cString: sqlite3_errmsg(database))
    }

    private func protectSQLiteFiles() throws {
        try LocalFilePrivacy.protectExistingItem(fileURL)
        try LocalFilePrivacy.protectExistingItem(URL(fileURLWithPath: fileURL.path + "-wal"))
        try LocalFilePrivacy.protectExistingItem(URL(fileURLWithPath: fileURL.path + "-shm"))
    }
}

private struct StoredDraft {
    var accountID: UUID
    var capture: LocalCapture
}

private struct StoredDraftPhoto {
    var position: Int
    var photo: CapturePhoto
}

private let transientDestructor = unsafeBitCast(-1, to: sqlite3_destructor_type.self)
