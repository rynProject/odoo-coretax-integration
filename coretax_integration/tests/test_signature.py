from odoo.tests import BaseCase, tagged

from ..tools.signature import compute_signature, verify_signature

SECRET = "whsec_test_secret"
BODY = b'{"id":"evt_1","type":"faktur.approved"}'


@tagged("post_install", "-at_install")
class TestWebhookSignature(BaseCase):

    def test_valid_signature(self):
        sig = compute_signature(SECRET, "1700000000", BODY)
        self.assertTrue(verify_signature(SECRET, f"t=1700000000,v1={sig}", BODY, now=1700000100))

    def test_rotation_accepts_any_v1(self):
        sig = compute_signature(SECRET, "1700000000", BODY)
        header = f"t=1700000000,v1={'0' * 64},v1={sig}"
        self.assertTrue(verify_signature(SECRET, header, BODY, now=1700000000))

    def test_rejects_tampered_body_wrong_secret_and_old_timestamp(self):
        sig = compute_signature(SECRET, "1700000000", BODY)
        header = f"t=1700000000,v1={sig}"
        self.assertFalse(verify_signature(SECRET, header, BODY + b" ", now=1700000000))
        self.assertFalse(verify_signature("whsec_other", header, BODY, now=1700000000))
        self.assertFalse(verify_signature(SECRET, header, BODY, now=1700000000 + 301))
        self.assertFalse(verify_signature(SECRET, "", BODY, now=1700000000))
        self.assertFalse(verify_signature("", header, BODY, now=1700000000))
