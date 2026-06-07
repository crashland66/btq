(function () {
  const DB_NAME = "field_capture_v1";
  const DB_VERSION = 1;
  const STORE = "captures";
  // Bump DB_VERSION + add an onupgradeneeded migration when
  // the schema changes. Never mutate this constant in place.

  let dbPromise = null;

  function openDb() {
    if (dbPromise) return dbPromise;
    dbPromise = new Promise((resolve, reject) => {
      const req = indexedDB.open(DB_NAME, DB_VERSION);
      req.onupgradeneeded = (event) => {
        const db = event.target.result;
        if (!db.objectStoreNames.contains(STORE)) {
          const store = db.createObjectStore(STORE, { keyPath: "capture_id" });
          store.createIndex("status", "status", { unique: false });
          store.createIndex("createdAt", "createdAt", { unique: false });
        }
      };
      req.onsuccess = () => {
        const db = req.result;
        db.onversionchange = () => {
          try {
            db.close();
          } catch (_e) {
            /* best effort */
          }
          dbPromise = null;
        };
        db.onclose = () => {
          dbPromise = null;
        };
        resolve(db);
      };
      req.onerror = () => {
        dbPromise = null;
        reject(req.error);
      };
      req.onblocked = () => {
        dbPromise = null;
        reject(new Error("indexeddb_blocked"));
      };
    });
    return dbPromise;
  }

  function isConnectionClosingError(err) {
    if (!err) return false;
    const name = err.name || "";
    const msg = (err.message || "").toLowerCase();
    return name === "InvalidStateError" || msg.includes("connection is closing");
  }

  async function withConnection(run) {
    try {
      return await run(await openDb());
    } catch (err) {
      if (!isConnectionClosingError(err)) throw err;
      dbPromise = null;
      try {
        return await run(await openDb());
      } catch (retryErr) {
        if (isConnectionClosingError(retryErr)) {
          const e = new Error("local_storage_restarted");
          e.name = "StorageRestartedError";
          e.cause = retryErr;
          throw e;
        }
        throw retryErr;
      }
    }
  }

  function reqToPromise(request) {
    return new Promise((resolve, reject) => {
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error);
    });
  }

  // Public API ------------------------------------------------

  async function putCapture(record) {
    // record shape:
    //   capture_id (string, primary key — pass through from buildJob)
    //   status ("pending" | "uploading" | "done" | "failed")
    //   attempts (number, default 0)
    //   lastError (string | null)
    //   createdAt (ISO string)
    //   lastTriedAt (ISO string | null)
    //   metadata (object — site_id, target_type, target_id, person_id, etc.)
    //   fields (object — form fields needed to rebuild FormData: site, qc_category, note, captured_at, exported_at, job_id, target_type, target_id, site_id)
    //   photos (array of { filename, mimeType, blob: Blob })
    //   audio  (null | { filename, mimeType, blob: Blob, durationSeconds })
    return withConnection((db) =>
      reqToPromise(db.transaction(STORE, "readwrite").objectStore(STORE).put(record)),
    );
  }

  async function getCapture(captureId) {
    return withConnection((db) =>
      reqToPromise(db.transaction(STORE, "readonly").objectStore(STORE).get(captureId)),
    );
  }

  async function listAll() {
    return withConnection((db) =>
      reqToPromise(db.transaction(STORE, "readonly").objectStore(STORE).getAll()),
    );
  }

  async function listByStatus(status) {
    return withConnection((db) => {
      const store = db.transaction(STORE, "readonly").objectStore(STORE);
      const index = store.index("status");
      return reqToPromise(index.getAll(status));
    });
  }

  async function deleteCapture(captureId) {
    return withConnection((db) =>
      reqToPromise(db.transaction(STORE, "readwrite").objectStore(STORE).delete(captureId)),
    );
  }

  async function updateCapture(captureId, patch) {
    // Read-modify-write inside a single transaction. Returns the
    // updated record. Throws if the row no longer exists.
    return withConnection(
      (db) =>
        new Promise((resolve, reject) => {
          const t = db.transaction(STORE, "readwrite");
          const store = t.objectStore(STORE);
          const getReq = store.get(captureId);
          getReq.onsuccess = () => {
            const existing = getReq.result;
            if (!existing) {
              reject(new Error("capture_not_found"));
              return;
            }
            const merged = Object.assign({}, existing, patch);
            const putReq = store.put(merged);
            putReq.onsuccess = () => resolve(merged);
            putReq.onerror = () => reject(putReq.error);
          };
          getReq.onerror = () => reject(getReq.error);
        }),
    );
  }

  async function countByStatus(status) {
    return withConnection((db) => {
      const store = db.transaction(STORE, "readonly").objectStore(STORE);
      const index = store.index("status");
      return reqToPromise(index.count(status));
    });
  }

  function isSupported() {
    try {
      return typeof indexedDB !== "undefined" && indexedDB !== null;
    } catch (_e) {
      return false;
    }
  }

  window.fieldCaptureDb = {
    DB_NAME,
    DB_VERSION,
    STORE,
    isSupported,
    putCapture,
    getCapture,
    listAll,
    listByStatus,
    deleteCapture,
    updateCapture,
    countByStatus,
  };
})();
