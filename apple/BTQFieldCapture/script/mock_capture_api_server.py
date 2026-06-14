#!/usr/bin/env python3
import json
import sys
from email.parser import BytesParser
from email.policy import default
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


TOKEN = "btq-smoke-token"


class CaptureAPIHandler(BaseHTTPRequestHandler):
    server_version = "BTQMockCaptureAPI/1.0"

    def do_GET(self):
        if not self.authorized():
            self.write_json({"error": "invalid_token"}, HTTPStatus.UNAUTHORIZED)
            return
        if self.path == "/api/session":
            self.write_json(
                {
                    "person": {"person_id": "mock_employee_1", "name": "Mock Employee"},
                    "token": {"token_id": "mock_token_1", "label": "Mock Smoke Token"},
                    "sites": [
                        {
                            "site_id": "mock_site_1",
                            "label": "Mock Site One",
                            "capture_guidance": "Capture a photo, voice note, and observation.",
                            "display_categories": [
                                {"value": "general_note", "label": "General note"},
                                {"value": "supplies", "label": "Supplies"},
                            ],
                        }
                    ],
                    "can_submit": True,
                    "can_review": False,
                    "max_images": 6,
                    "inbox_count": 0,
                },
                HTTPStatus.OK,
            )
            return
        if self.path == "/api/my-submissions":
            self.write_json(
                {
                    "submissions": [
                        {
                            "capture_id": "cap-native-smoke-history",
                            "site_id": "mock_site_1",
                            "site_name": "Mock Site One",
                            "target_type": "location",
                            "target_id": "mock_site_1",
                            "captured_at": "2026-06-14T10:15:00Z",
                            "photo_count": 1,
                            "has_audio": True,
                            "has_text_note": True,
                            "note_text": "Native mock API smoke",
                            "photo_urls": ["/media/mock_photo_1"],
                            "track": "B",
                            "stage": "reviewed",
                            "retargetable": False,
                            "outcome_label": "No action needed",
                            "per_photo_quality": [
                                {
                                    "severity": "ok",
                                    "flags": [],
                                    "description": "Readable smoke photo",
                                    "possible_issues": [],
                                }
                            ],
                        }
                    ],
                    "quality_summary": {
                        "total_processed": 1,
                        "clear": 1,
                        "flag_counts": {},
                    },
                },
                HTTPStatus.OK,
            )
            return
        self.write_json({"error": "not_found"}, HTTPStatus.NOT_FOUND)

    def do_POST(self):
        if self.path != "/api/submit":
            self.write_json({"error": "not_found"}, HTTPStatus.NOT_FOUND)
            return
        if not self.authorized():
            self.write_json({"error": "invalid_token"}, HTTPStatus.UNAUTHORIZED)
            return
        try:
            fields, files = self.read_multipart()
            self.validate_submit(fields, files)
        except SubmissionError as error:
            self.write_json({"error": error.code, "message": error.message}, error.status)
            return
        self.write_json(
            {
                "status": "submitted",
                "job_id": fields["job_id"],
                "capture_id": fields["capture_id"],
                "couchdb_doc_id": fields["capture_id"],
                "photo_count": len(files.get("photos", [])),
                "audio_count": len(files.get("audio", [])),
                "idempotent_replay": False,
            },
            HTTPStatus.CREATED,
        )

    def authorized(self):
        return self.headers.get("Authorization") == f"Bearer {TOKEN}"

    def read_multipart(self):
        content_type = self.headers.get("Content-Type", "")
        if not content_type.startswith("multipart/form-data;"):
            raise SubmissionError("invalid_content_type", "Expected multipart/form-data")
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        message = BytesParser(policy=default).parsebytes(
            b"Content-Type: " + content_type.encode("utf-8") + b"\r\n\r\n" + body
        )
        if not message.is_multipart():
            raise SubmissionError("invalid_multipart", "Expected multipart body")

        fields = {}
        files = {}
        for part in message.iter_parts():
            name = part.get_param("name", header="content-disposition")
            filename = part.get_filename()
            payload = part.get_payload(decode=True) or b""
            if not name:
                continue
            if filename:
                files.setdefault(name, []).append(
                    {
                        "filename": filename,
                        "content_type": part.get_content_type(),
                        "size": len(payload),
                    }
                )
            else:
                fields[name] = payload.decode("utf-8")
        return fields, files

    def validate_submit(self, fields, files):
        required_fields = [
            "job_id",
            "capture_id",
            "site",
            "site_id",
            "target_type",
            "target_id",
            "qc_category",
            "note",
            "captured_at",
            "exported_at",
            "metadata_json",
            "photo_notes_json",
            "audio_duration_seconds",
        ]
        for field in required_fields:
            if not str(fields.get(field, "")).strip():
                raise SubmissionError("missing_field", f"Missing multipart field: {field}")

        if fields["site_id"] != "mock_site_1" or fields["qc_category"] != "general_note":
            raise SubmissionError("invalid_field", "Unexpected site or category")

        photos = files.get("photos", [])
        audio = files.get("audio", [])
        if len(photos) != 1 or photos[0]["filename"] != "smoke-photo.jpg" or photos[0]["size"] <= 0:
            raise SubmissionError("missing_photo", "Expected one non-empty smoke photo")
        if len(audio) != 1 or audio[0]["filename"] != "smoke-voice.m4a" or audio[0]["size"] <= 0:
            raise SubmissionError("missing_audio", "Expected one non-empty smoke audio file")

        try:
            metadata = json.loads(fields["metadata_json"])
            photo_notes = json.loads(fields["photo_notes_json"])
        except json.JSONDecodeError as error:
            raise SubmissionError("invalid_json", f"Invalid native metadata JSON: {error}") from error

        if metadata.get("client") != "btq_native_apple" or metadata.get("has_audio") is not True:
            raise SubmissionError("invalid_metadata", "Native metadata is missing expected client/audio fields")
        if not photo_notes or photo_notes[0].get("filename") != "smoke-photo.jpg":
            raise SubmissionError("invalid_photo_notes", "Photo notes did not reference the smoke photo")

    def write_json(self, payload, status):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        sys.stderr.write("mock-capture-api: " + format % args + "\n")


class SubmissionError(Exception):
    def __init__(self, code, message, status=HTTPStatus.BAD_REQUEST):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def main():
    if len(sys.argv) != 2:
        raise SystemExit("usage: mock_capture_api_server.py <port-file>")
    server = ThreadingHTTPServer(("127.0.0.1", 0), CaptureAPIHandler)
    port = server.server_address[1]
    with open(sys.argv[1], "w", encoding="utf-8") as handle:
        handle.write(str(port))
    server.serve_forever()


if __name__ == "__main__":
    main()
