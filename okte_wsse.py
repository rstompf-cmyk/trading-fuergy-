# -*- coding: utf-8 -*-
"""
okte_wsse.py — WS-Security XML Digital Signature helper pre OKTE SOAP IdmOrderBook.

Niektoré SOAP endpointy (vrátane OKTE IdmOrderBook) vyžadujú aby SOAP envelope
mala WS-Security `<ds:Signature>` element ktorý podpisuje:
  - `<s:Body>` (cez wsu:Id="Body-1")
  - `<u:Timestamp>` (cez u:Id="TS-1")

Podpis je RSA-SHA256, digest SHA-256, kanonikalizácia Exclusive C14N
(http://www.w3.org/2001/10/xml-exc-c14n#). Cert je X.509 v PEM formáte
(rovnaký ktorý sa používa pre mTLS na :8443).

Verejné API:
  sign_soap_envelope(soap_xml: str, cert_pem_path: str, key_pem_path: str) -> str
      Vráti SOAP envelope ako string so vloženou Signature.

Predpoklady na vstupný `soap_xml`:
  - SOAP 1.2 envelope (xmlns:s="http://www.w3.org/2003/05/soap-envelope")
  - <s:Header><o:Security> obsahuje <u:Timestamp u:Id="TS-1"> + <o:UsernameToken u:Id="UT-1">
  - <s:Body> bez existujúceho wsu:Id (helper ho doplní)
"""
from __future__ import annotations
import base64
import uuid
from typing import Optional
from lxml import etree


# Namespace constants
NS_SOAP = "http://www.w3.org/2003/05/soap-envelope"
NS_WSSE = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd"
NS_WSU = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd"
NS_DS = "http://www.w3.org/2000/09/xmldsig#"
NS_EXC_C14N = "http://www.w3.org/2001/10/xml-exc-c14n#"
NS_WSA = "http://schemas.xmlsoap.org/ws/2004/08/addressing"

# WS-Security profile constants
X509_PROFILE = ("http://docs.oasis-open.org/wss/2004/01/oasis-200401-"
                  "wss-x509-token-profile-1.0#X509v3")
BASE64_ENC = ("http://docs.oasis-open.org/wss/2004/01/oasis-200401-"
                "wss-soap-message-security-1.0#Base64Binary")


def _load_cert_der_base64(pem_path: str) -> str:
    """Načíta X.509 cert z PEM súboru a vráti DER bytes ako base64 string."""
    from cryptography.x509 import load_pem_x509_certificate
    from cryptography.hazmat.primitives.serialization import Encoding
    with open(pem_path, "rb") as f:
        pem = f.read()
    cert = load_pem_x509_certificate(pem)
    der = cert.public_bytes(Encoding.DER)
    return base64.b64encode(der).decode("ascii")


def _load_private_key(key_path: str):
    """Načíta RSA private key z PEM súboru. Akceptuje aj passphrase=None."""
    from cryptography.hazmat.primitives.serialization import load_pem_private_key
    with open(key_path, "rb") as f:
        pem = f.read()
    return load_pem_private_key(pem, password=None)


def _exc_c14n(elem, inclusive_ns_prefixes=None) -> bytes:
    """Exclusive XML Canonicalization (C14N) podľa http://www.w3.org/2001/10/xml-exc-c14n#

    Args:
        inclusive_ns_prefixes: list of namespace prefixes to include from parent scope
            (napr. ["s", "a"]). WCF AsymmetricBinding to typicky vyžaduje pre elementy
            čo používajú prefix-y deklarované na envelope úrovni.
    """
    return etree.tostring(
        elem,
        method="c14n",
        exclusive=True,
        with_comments=False,
        inclusive_ns_prefixes=inclusive_ns_prefixes or [],
    )


def _sha1_digest_b64(data: bytes) -> str:
    """SHA-1 digest + base64 encode (pre WCF Basic256 suite)."""
    from cryptography.hazmat.primitives import hashes
    digest = hashes.Hash(hashes.SHA1())
    digest.update(data)
    return base64.b64encode(digest.finalize()).decode("ascii")


def _sha256_digest_b64(data: bytes) -> str:
    """SHA-256 digest + base64 encode (pre Basic256Sha256 suite)."""
    from cryptography.hazmat.primitives import hashes
    digest = hashes.Hash(hashes.SHA256())
    digest.update(data)
    return base64.b64encode(digest.finalize()).decode("ascii")


def _rsa_sha1_sign(private_key, data: bytes) -> str:
    """RSA-SHA1 podpis + base64 encode (pre WCF Basic256 suite)."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    signature = private_key.sign(
        data,
        padding.PKCS1v15(),
        hashes.SHA1(),
    )
    return base64.b64encode(signature).decode("ascii")


def _rsa_sha256_sign(private_key, data: bytes) -> str:
    """RSA-SHA256 podpis + base64 encode (pre Basic256Sha256 suite)."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    signature = private_key.sign(
        data,
        padding.PKCS1v15(),
        hashes.SHA256(),
    )
    return base64.b64encode(signature).decode("ascii")


def _qname(ns: str, local: str) -> str:
    """ElementTree Clark notation: {namespace}localname."""
    return f"{{{ns}}}{local}"


def sign_soap_envelope(soap_xml: str, cert_pem_path: str,
                        key_pem_path: str,
                        body_id: str = "Body-1",
                        ts_id: str = "TS-1",
                        bst_id: Optional[str] = None,
                        sig_id: Optional[str] = None,
                        algorithm_suite: str = "Basic256") -> str:
    """Vloží WS-Security X.509 digital signature do SOAP envelope.

    Args:
        soap_xml: SOAP 1.2 envelope ako string. Musí obsahovať Timestamp s u:Id=ts_id.
        cert_pem_path: Cesta k X.509 cert v PEM formáte.
        key_pem_path: Cesta k RSA private key v PEM formáte.
        body_id: Identifier ktorý sa pridá na <s:Body> (default "Body-1").
        ts_id: Identifier <u:Timestamp> ktorý sa bude podpisovať (default "TS-1").
        bst_id: Identifier <o:BinarySecurityToken> (default auto-generated).
        sig_id: Identifier <ds:Signature> (default auto-generated).

    Returns:
        Podpísané SOAP envelope ako string (UTF-8).

    Raises:
        ValueError ak envelope nemá očakávanú štruktúru (Security, Timestamp, Body).
    """
    bst_id = bst_id or f"X509-{uuid.uuid4().hex[:8]}"
    sig_id = sig_id or f"SIG-{uuid.uuid4().hex[:8]}"

    # Algorithm suite — WCF Basic256 vs Basic256Sha256
    if algorithm_suite.lower() == "basic256sha256":
        sig_method_uri = "http://www.w3.org/2001/04/xmldsig-more#rsa-sha256"
        digest_method_uri = "http://www.w3.org/2001/04/xmlenc#sha256"
        digest_fn = _sha256_digest_b64
        sign_fn = _rsa_sha256_sign
    else:   # Basic256 (default — SHA-1 podľa WSS 1.0)
        sig_method_uri = "http://www.w3.org/2000/09/xmldsig#rsa-sha1"
        digest_method_uri = "http://www.w3.org/2000/09/xmldsig#sha1"
        digest_fn = _sha1_digest_b64
        sign_fn = _rsa_sha1_sign

    # Parse envelope
    parser = etree.XMLParser(remove_blank_text=False)
    root = etree.fromstring(soap_xml.encode("utf-8"), parser)

    # Nájdi <s:Body>
    body = root.find(_qname(NS_SOAP, "Body"))
    if body is None:
        raise ValueError("SOAP envelope nemá <s:Body>")

    # Nájdi <s:Header>
    header = root.find(_qname(NS_SOAP, "Header"))
    if header is None:
        raise ValueError("SOAP envelope nemá <s:Header>")

    # Nájdi <o:Security>
    security = header.find(_qname(NS_WSSE, "Security"))
    if security is None:
        raise ValueError("Header nemá <o:Security>")

    # Nájdi <u:Timestamp u:Id="TS-...">
    timestamp = security.find(_qname(NS_WSU, "Timestamp"))
    if timestamp is None:
        raise ValueError("Security nemá <u:Timestamp>")
    # Force-set wsu:Id na ts_id (existujúci sa zachová)
    existing_ts_id = timestamp.get(_qname(NS_WSU, "Id"))
    if existing_ts_id:
        ts_id = existing_ts_id

    # Force-set wsu:Id na Body
    body.set(_qname(NS_WSU, "Id"), body_id)

    # WSDL policy SignedParts vyžaduje podpis aj WS-Addressing headers:
    # Body + To + ReplyTo + MessageID + Action (a UsernameToken ako SignedSupportingToken).
    # Tuple: (element, id, label, inclusive_ns_prefixes_for_c14n)
    # WS-A headers majú atribút s:mustUnderstand="1" → musíme zahrnúť "s" prefix
    # z envelope scope pri C14N (inak digest nesedí so server-side).
    elements_to_sign = []
    elements_to_sign.append((body, body_id, "Body", []))
    elements_to_sign.append((timestamp, ts_id, "Timestamp", []))

    wsa_header_names = ("Action", "To", "MessageID", "ReplyTo", "From", "FaultTo", "RelatesTo")
    wsa_id_counter = 1
    for name in wsa_header_names:
        el = header.find(_qname(NS_WSA, name))
        if el is None:
            continue
        existing_id = el.get(_qname(NS_WSU, "Id"))
        if existing_id:
            el_id = existing_id
        else:
            el_id = f"WSA-{name}-{wsa_id_counter}"
            wsa_id_counter += 1
            el.set(_qname(NS_WSU, "Id"), el_id)
        # WS-A elementy môžu mať s:mustUnderstand attr → zahrnúť "s" prefix
        elements_to_sign.append((el, el_id, name, ["s"]))

    # UsernameToken (SignedSupportingToken podľa WSDL)
    ut = security.find(_qname(NS_WSSE, "UsernameToken"))
    if ut is not None:
        existing_ut_id = ut.get(_qname(NS_WSU, "Id"))
        if existing_ut_id:
            ut_id = existing_ut_id
        else:
            ut_id = "UT-1"
            ut.set(_qname(NS_WSU, "Id"), ut_id)
        elements_to_sign.append((ut, ut_id, "UsernameToken", []))

    # 1. Vytvor BinarySecurityToken element
    cert_b64 = _load_cert_der_base64(cert_pem_path)
    bst = etree.SubElement(security, _qname(NS_WSSE, "BinarySecurityToken"))
    bst.set("EncodingType", BASE64_ENC)
    bst.set("ValueType", X509_PROFILE)
    bst.set(_qname(NS_WSU, "Id"), bst_id)
    bst.text = cert_b64

    # 2. Postaviť SignedInfo
    nsmap_ds = {"ds": NS_DS}
    signed_info = etree.Element(_qname(NS_DS, "SignedInfo"), nsmap=nsmap_ds)

    c14n_method = etree.SubElement(signed_info, _qname(NS_DS, "CanonicalizationMethod"))
    c14n_method.set("Algorithm", NS_EXC_C14N)

    sig_method = etree.SubElement(signed_info, _qname(NS_DS, "SignatureMethod"))
    sig_method.set("Algorithm", sig_method_uri)

    # Reference per signed element. Pre prvky čo používajú namespace prefix
    # z envelope scope (napr. s:mustUnderstand) pridáme <ec:InclusiveNamespaces
    # PrefixList="..."/> do Transform aby digest sedel so server-side C14N.
    for el, el_id, _label, inc_prefixes in elements_to_sign:
        el_c14n = _exc_c14n(el, inclusive_ns_prefixes=inc_prefixes)
        el_digest = digest_fn(el_c14n)
        ref = etree.SubElement(signed_info, _qname(NS_DS, "Reference"))
        ref.set("URI", f"#{el_id}")
        transforms = etree.SubElement(ref, _qname(NS_DS, "Transforms"))
        trans = etree.SubElement(transforms, _qname(NS_DS, "Transform"))
        trans.set("Algorithm", NS_EXC_C14N)
        if inc_prefixes:
            inc_ns = etree.SubElement(trans, _qname(NS_EXC_C14N, "InclusiveNamespaces"))
            inc_ns.set("PrefixList", " ".join(inc_prefixes))
        dm = etree.SubElement(ref, _qname(NS_DS, "DigestMethod"))
        dm.set("Algorithm", digest_method_uri)
        dv = etree.SubElement(ref, _qname(NS_DS, "DigestValue"))
        dv.text = el_digest

    # 4. Kanonikalizovať SignedInfo a podpísať
    signed_info_c14n = _exc_c14n(signed_info)
    private_key = _load_private_key(key_pem_path)
    signature_b64 = sign_fn(private_key, signed_info_c14n)

    # 5. Postaviť Signature element
    signature = etree.Element(_qname(NS_DS, "Signature"), nsmap=nsmap_ds)
    signature.set("Id", sig_id)
    signature.append(signed_info)

    sig_value = etree.SubElement(signature, _qname(NS_DS, "SignatureValue"))
    sig_value.text = signature_b64

    key_info = etree.SubElement(signature, _qname(NS_DS, "KeyInfo"))
    sec_token_ref = etree.SubElement(key_info, _qname(NS_WSSE, "SecurityTokenReference"))
    ref = etree.SubElement(sec_token_ref, _qname(NS_WSSE, "Reference"))
    ref.set("URI", f"#{bst_id}")
    ref.set("ValueType", X509_PROFILE)

    # 6. Vložiť Signature do Security (za BinarySecurityToken, pred UsernameToken)
    # Order odporúčaný WS-Security: Timestamp → BinarySecurityToken → Signature → UsernameToken
    security.append(signature)   # na koniec; OKTE by malo akceptovať

    # Serialize späť na string
    return etree.tostring(root, encoding="utf-8", xml_declaration=True).decode("utf-8")


if __name__ == "__main__":
    # Sample test — len buildne signed envelope a vytlačí (žiadne sieťové volanie)
    sample = '''<?xml version="1.0" encoding="utf-8"?>
<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"
            xmlns:u="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd">
  <s:Header>
    <o:Security xmlns:o="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd">
      <u:Timestamp u:Id="TS-1">
        <u:Created>2026-06-01T22:00:00.000Z</u:Created>
        <u:Expires>2026-06-01T22:05:00.000Z</u:Expires>
      </u:Timestamp>
    </o:Security>
  </s:Header>
  <s:Body>
    <Hello>World</Hello>
  </s:Body>
</s:Envelope>'''
    import sys
    if len(sys.argv) > 2:
        signed = sign_soap_envelope(sample, sys.argv[1], sys.argv[2])
        print(signed)
    else:
        print("Usage: python3 okte_wsse.py <cert.pem> <key.pem>")
