# Stato backup e ripristino — Archivio CTS

Aggiornato: 30 ottobre 2026 (Europe/Rome). Questa è una fotografia dei backup disponibili; non significa che il minisito sia ripristinato o che l'archivio sia indicizzabile integralmente.

## Copie disponibili

- **Codice e istruzioni agente:** repository privata GitHub [`lvogruppo/cts-archive-backup`](https://github.com/lvogruppo/cts-archive-backup), branch `main`. Contiene codice, test, script di copia, `AGENT_INSTRUCTIONS.md`, `CURRENT_DECISIONS.md` e questa scheda. La working copy sorgente era sul commit `60a9d74f2120e868b092b2bf73b30f2177d9cda9`; sono inclusi i file tracciati e non ignorati presenti nel workspace al 30 ottobre. La suite isolata è passata con 33 test.
- **Database MySQL CTS:** dump logico cifrato `CTS-MySQL-Backup-2026-10-29.sql.gz.gpg` nella [cartella privata Google Drive](https://drive.google.com/drive/folders/1xgAkxDwoPI4fpJtzUNkAAMPMYj_VH0ie). Dimensione 96.197.449 byte. SHA-256 `559d91431c9fa8c3fec98998810f54632d3a275e22e400ff4a38271f483eb5c1`. La decifratura e l'integrità gzip sono state verificate in streaming prima dell'upload. La passphrase non è archiviata qui: conservarla separatamente.
- **PDF originali:** copia privata completa nella [cartella Drive CTS-PDF-Archive-2026-10-30](https://drive.google.com/drive/folders/1H6Owh6jz429-NV7oDQ3brzXdnX-W-G45), proprietà dell'account Lvo Gruppo. Sono 433 file per 3.054.813.500 byte. I 433 originali locali sono stati verificati per dimensione e MD5 rispetto a manifesto e Drive; la copia Drive è stata poi riconciliata rispetto all'origine per nome, dimensione e MD5. Il manifesto dettagliato e i checksum sono archiviati nella stessa cartella.
- **Documentazione e input:** le istruzioni agente, le decisioni correnti, la scheda di ripristino, il manifesto originale e trasformato e il report degli errori download (vuoto) sono salvati nella cartella Drive privata principale e/o nella sottocartella PDF. Non sono inseriti nel database né nei PDF.

## Stato effettivo dei dati indicizzati

Al controllo del 30 ottobre 2026 il DB conteneva 433 fonti, 433 documenti, 20.614 pagine e 34.015 chunk; i conteggi aggregati coincidevano con quelli del dump del 29 ottobre. Gli stati attuali erano 355 documenti `processed`, 77 `partial`, 1 `processing`; pagine: 20.534 `processed` e 80 `empty`. Questo non prova una copertura completa. Le pagine vuote non sono automaticamente verificate e la consultazione dei verbali deve restare bloccata fino alla risoluzione o revisione delle eccezioni e al superamento del gate.

## Elementi non inclusi o da sistemare

- Nessun segreto in chiaro è stato copiato. La chiave Resend `CTS Archivio OTP` è stata revocata; non c'è una chiave valida nel backup. Il mittente scelto è `Archivio CTS <info@lvogruppo.it>`; servirà una nuova chiave con permesso minimo e salvataggio diretto nei segreti runtime del corretto progetto WebDev.
- Questo backup manuale non è un'esportazione ufficiale Manus **Task Data** e non comprende configurazione WebDev/hosting, dominio/binding, segreti runtime, integrazioni o capability gestite. Non è stato trovato un pacchetto Task Data nelle directory di download di questo ambiente. Se disponibile nell'account Manus, il Task Data export ufficiale è un ulteriore backup da conservare in modo protetto.
- Il codice salvato presenta una discrepanza da correggere prima del rilascio: `POST /api/auth/verify-code` richiede ancora `consultation_ready`, perciò la verifica OTP non funziona mentre l'archivio è bloccato; alcuni messaggi UI e passaggi di documentazione parlano ancora di email autorizzate. La decisione più recente è invece OTP per qualsiasi email valida, con login funzionante subito dopo la configurazione Resend e consultazione dei verbali bloccata fino a copertura completa. Vedi `AGENT_INSTRUCTIONS.md` e `CURRENT_DECISIONS.md`.

## Ripristino sicuro

Ripristinare il dump solo in uno schema MySQL vuoto, dopo aver controllato SHA-256 e ottenuto la passphrase separatamente dall'utente; importare in streaming senza scrivere SQL in chiaro su disco. Recuperare prima il progetto WebDev CTS originale o un Task Data export, se disponibile; non creare un duplicato per aggirare un problema di binding. Reconfigurare i segreti nel progetto corretto e verificare che OTP e gate documentale operino separatamente prima di pubblicare.
