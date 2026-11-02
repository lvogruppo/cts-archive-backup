from __future__ import annotations

import argparse
import sys

ROOT = __import__("pathlib").Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.database import AllowedEmail, SessionLocal, create_schema  # noqa: E402
from app.security import normalize_email, valid_email  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Aggiunge un indirizzo autorizzato all'Archivio CTS.")
    parser.add_argument("--add", required=True, help="Indirizzo email da autorizzare.")
    parser.add_argument("--label", help="Etichetta interna facoltativa.")
    args = parser.parse_args()
    email = normalize_email(args.add)
    if not valid_email(email):
        print("Indirizzo email non valido.", file=sys.stderr)
        return 2
    if SessionLocal is None:
        print("DATABASE_URL non configurato.", file=sys.stderr)
        return 2

    create_schema()
    with SessionLocal() as db:
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
