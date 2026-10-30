# CTS source snapshot

Backup privato del codice sorgente e della documentazione dell'applicazione di consultazione dell'archivio CTS. Aggiornato il 30 ottobre 2026.

La snapshot è stata ricostruita dai file tracciati e non ignorati della working copy `/home/ubuntu/ctsrag`, partendo dal commit locale `60a9d74f2120e868b092b2bf73b30f2177d9cda9` e includendo le modifiche/test presenti nella working copy alla data di aggiornamento. Il branch di backup è `main` della repository privata `lvogruppo/cts-archive-backup`.

Include sorgenti FastAPI, template e asset statici, file di configurazione non segreti, documentazione (`AGENT_INSTRUCTIONS.md`, `CURRENT_DECISIONS.md`, `RESTORE_STATE.md`), script e test. La suite isolata è passata il 30 ottobre 2026 con 33 test; `compileall` e `git diff --check` sono passati.

Esclude dump/database, testo estratto e vettori, PDF originali, manifesto dei file, directory dati locali, virtualenv, cache, file `.env`, chiavi, token e passphrase. I 433 PDF e il manifesto MD5 sono archiviati separatamente nella cartella Google Drive privata CTS; il database è conservato come dump cifrato nella stessa area privata.

Questa è una fotografia puntuale, non una sincronizzazione continua con il progetto WebDev o la working copy. Le informazioni aggiornate su copertura, credenziali mancanti e limiti di ripristino sono in `RESTORE_STATE.md`; non trattare i piani storici come prova che il servizio sia pronto.
