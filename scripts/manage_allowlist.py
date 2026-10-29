from __future__ import annotations

import argparse
import sys

ROOT = __import__("pathlib").Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.database import AllowedDomain, AllowedEmail, SessionLocal, create_schema  # noqa: E402
from app.security import normalize_domain, normalize_email, valid_domain, valid_email  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Aggiunge un indirizzo o dominio autorizzato all'Archivio CTS.")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--add", help="Indirizzo email da autorizzare.")
    target.add_argument("--add-domain", help="Dominio email esatto da autorizzare, senza wildcard.")
    parser.add_argument("--label", help="Etichetta interna facoltativa.")
    args = parser.parse_args()
    if SessionLocal is None:
        print("DATABASE_URL non configurato.", file=sys.stderr)
        return 2

    email = normalize_email(args.add) if args.add else None
    domain = normalize_domain(args.add_domain) if args.add_domain else None
    if email and not valid_email(email):
        print("Indirizzo email non valido.", file=sys.stderr)
        return 2
    if domain and not valid_domain(domain):
        print("Dominio email non valido.", file=sys.stderr)
        return 2

    create_schema()
    with SessionLocal() as db:
        if domain:
            entry = db.query(AllowedDomain).filter(AllowedDomain.domain == domain).first()
            if entry:
                if args.label is not None:
                    entry.label = args.label[:160]
            else:
                db.add(AllowedDomain(domain=domain, label=args.label[:160] if args.label else None))
            db.commit()
            print("domain_allowlist_entry_saved")
            return 0

        assert email is not None
        entry = db.query(AllowedEmail).filter(AllowedEmail.email == email).first()
        if entry:
            if args.label is not None:
                entry.label = args.label[:160]
        else:
            db.add(AllowedEmail(email=email, label=args.label[:160] if args.label else None))
        db.commit()
    print("allowlist_entry_saved")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
