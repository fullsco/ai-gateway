"""Controlled TLS receiver fixture; no external destination and no tool execution."""

import hashlib
import hmac
import json
import ssl
import time
from collections import Counter
from datetime import UTC, datetime, timedelta

import httpx
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from fastapi import FastAPI, Request
from fastapi.responses import Response

HOST = "webhook.qualification.test"
URL = f"https://{HOST}/alerts"
FIELDS = {"version", "event_id", "alert_id", "sequence", "status", "previous_status",
          "severity", "occurred_at"}


def certificates(directory):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, HOST)])
    now = datetime.now(UTC)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=1))
            .not_valid_after(now + timedelta(hours=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(HOST)]), critical=False)
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .sign(key, hashes.SHA256()))
    cert_path, key_path = directory / "receiver.pem", directory / "receiver-key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    key_path.chmod(0o600)
    return cert_path, key_path


class LoopbackTLS(httpx.AsyncBaseTransport):
    """Pin a test-only origin to the receiver socket, retaining TLS hostname verification."""

    def __init__(self, port, certificate):
        self.port = port
        self.transport = httpx.AsyncHTTPTransport(
            verify=ssl.create_default_context(cafile=str(certificate)), retries=0,
        )

    async def handle_async_request(self, request):
        if (request.url.scheme != "https" or request.url.host != HOST
                or request.url.port not in (None, 443) or request.url.path != "/alerts"):
            raise RuntimeError("Receiver transport rejects all other destinations")
        mapped = httpx.Request(request.method,
            request.url.copy_with(host="127.0.0.1", port=self.port), headers=request.headers,
            stream=request.stream, extensions={**request.extensions, "sni_hostname": HOST})
        return await self.transport.handle_async_request(mapped)

    async def aclose(self):
        await self.transport.aclose()


class Receiver:
    def __init__(self, secret):
        self.secret = secret.encode()
        self.attempts = Counter()
        self.accepted = {}
        self.duplicates = 0
        self.rejected = 0
        self.mode = "retry_once"

    def receive(self, body, headers):
        try:
            timestamp = headers["x-gateway-timestamp"]
            signature = headers["x-gateway-signature"]
            if len(body) > 2048 or abs(time.time() - int(timestamp)) > 60:
                raise ValueError
            expected = "sha256=" + hmac.new(
                self.secret, timestamp.encode() + b"." + body, hashlib.sha256,
            ).hexdigest()
            if not hmac.compare_digest(signature, expected):
                raise ValueError
            payload = json.loads(body)
            event_id = payload["event_id"]
            if (set(payload) != FIELDS or payload["version"] != 1
                    or event_id != headers["x-gateway-event-id"]
                    or payload["status"] not in {"open", "resolved"}):
                raise ValueError
        except (ValueError, KeyError, TypeError):
            self.rejected += 1
            return 401
        self.attempts[event_id] += 1
        if self.mode == "permanent":
            return 400
        if self.mode == "exhaust":
            return 503
        if self.mode == "retry_once" and self.attempts[event_id] == 1:
            return 503
        fingerprint = hashlib.sha256(body).hexdigest()
        if event_id in self.accepted:
            if self.accepted[event_id]["body_sha256"] != fingerprint:
                return 409
            self.duplicates += 1
        else:
            self.accepted[event_id] = {"body_sha256": fingerprint,
                "sequence": payload["sequence"], "status": payload["status"]}
        return 204

    def app(self):
        app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

        @app.post("/alerts")
        async def alert(request: Request):
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > 2048:
                    return Response(status_code=413)
            return Response(status_code=self.receive(bytes(body), request.headers))

        return app
