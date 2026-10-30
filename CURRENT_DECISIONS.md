# Decisioni correnti per il ripristino CTS

Questo file integra e, sui punti qui indicati, prevale sulle descrizioni storiche in `plan.md`, `README.md` e `TODO.md` della snapshot. È stato redatto il 30 ottobre 2026 sulla base delle conferme esplicite dell'utente.

## Accesso
L'utente ha deciso accesso OTP via email per **qualsiasi indirizzo valido**, senza allowlist o approvazione preventiva. La richiesta e la verifica dell'OTP devono funzionare appena Resend è configurato, anche mentre i contenuti restano bloccati. L'autenticazione non apre l'archivio: tutte le API, pagine e risposte che espongono contenuti CTS devono continuare a restituire un rifiuto server-side finché non è provata la copertura integrale. La precedente chiave Resend è stata revocata; non c'è una chiave valida inclusa nel backup.

Mittente richiesto: `Archivio CTS <info@lvogruppo.it>`. Va creata una nuova chiave Resend Sending access, limitata al dominio mittente `lvogruppo.it`, e inserita nel corretto progetto come segreto protetto. La restrizione è sul dominio mittente, non sui destinatari: i destinatari possono essere qualsiasi indirizzo valido.

## Stato effettivo della fotografia
Il dump DB del 29 ottobre 2026 è una fotografia con 433 fonti, 433 documenti, 20.614 pagine e 34.015 chunk. Il 30 ottobre i conteggi aggregati correnti coincidevano; gli stati erano 355 documenti `processed`, 77 `partial`, 1 `processing` e pagine 20.534 `processed` più 80 `empty`. Questo non certifica copertura completa: finché gli stati parziali, l'elaborazione interrotta e le pagine vuote non sono chiariti, il gate deve restare chiuso.

Il template attualmente salvato contiene ancora frasi che implicano un indirizzo autorizzato e `app/main.py` condiziona la verifica OTP a `consultation_ready`. Sono discrepanze note da correggere prima di dichiarare il login pronto. La verifica OTP deve creare una sessione mentre l'archivio è bloccato; le route di consultazione devono restare bloccate.

## Conservazione
La repository privata GitHub contiene solo codice e documentazione. I PDF, il manifesto e il dump DB appartengono al backup privato Drive. Passphrase e segreti runtime restano fuori da GitHub e dai file non cifrati. Il backup manuale non contiene esportazione ufficiale Manus Task Data, configurazione hosting/WebDev, credenziali o integrazioni gestite. Preferire il recupero del progetto CTS WebDev esistente o il Task Data export Manus se disponibile, invece di creare un duplicato.
