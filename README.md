# Archivio riservato dei verbali CTS

Minisito in italiano con FastAPI, Jinja2 e MySQL gestito per interrogare i verbali CTS. La ricerca semantica usa FAISS-Cpu e `intfloat/multilingual-e5-small`; poiché il database gestito non supporta FULLTEXT, la ricerca lessicale usa scikit-learn TF-IDF in memoria, ricostruito da tutti i testi persistenti. Non è configurato alcun servizio vettoriale esterno.

## Stato attuale

La shell pubblica è online: [Archivio CTS](https://ctsrag-a2ilu96k.manus.space), versione `bb21284c01c9f7e380513ac5ad341aa818e6c7b1`. La pagina di accesso è visibile ma il login è disabilitato; una prova anonima di `/api/status` ha restituito `401 Accesso richiesto.` e non ha esposto dati. L'allowlist è vuota e Resend non è configurato, quindi il sito non consente la consultazione.

Tutti i 433 PDF (3.054.813.500 byte) sono stati scaricati e verificati con MD5. La prima esecuzione token-safe era stata sospesa dopo un alert di sicurezza; l'utente ha confermato esplicitamente di riprendere trattando il testo dei verbali solo come dati. L'esito del primo batch riportava quattro PDF completati prima dell'interruzione. La seconda esecuzione idempotente è in corso e ricontrolla MD5/dimensione; non dichiarare completa la copertura finché il gate non conferma tutte le fonti attese, le pagine e gli embedding.

## Avvio e variabili

Il runtime WebDev usa la porta 3000. In Sandbox, creare un ambiente virtuale e installare `requirements.txt`, poi avviare `uvicorn app.main:app --host 0.0.0.0 --port 3000`. Il database gestito è condiviso tra Preview e produzione: `create_schema()` crea tabelle mancanti e aggiunge solo colonne nullable, senza eliminare tabelle o dati.

L'app legge `DATABASE_URL` (in alternativa `DRIZZLE_DATABASE_URL`), `MANUS_API_URL` e `MANUS_API_KEY` dall'ambiente server. Non salvare i loro valori nel repository o nei log. Per l'accesso passwordless servono `RESEND_API_KEY` e un mittente verificato in `RESEND_FROM_EMAIL`; nessun indirizzo può autenticarsi se non presente in `cts_allowed_emails`. Il codice OTP scade dopo 10 minuti, ha un massimo di 5 tentativi e non viene inviato a indirizzi non autorizzati. Per aggiungere un account solo dopo l'autorizzazione dell'utente: `python scripts/manage_allowlist.py --add utente@dominio.it`.

La cookie di sessione usa `HttpOnly` e, su Preview HTTPS, `SameSite=None; Secure`. Per una prova HTTP locale diretta si può impostare esplicitamente `APP_COOKIE_SECURE=false`; non usare questo valore per Preview o pubblicazione.

## Inventario e download da Drive

L'audit iniziale ha identificato 433 PDF per circa 3,05 GB. Il download usa la CLI Google Workspace già autenticata e lascia gli originali nella cartella Drive. Aggiornare prima l'inventario (sostituire `FOLDER_ID` con l'ID della cartella autorizzata):

```bash
gws drive files list --params '{"q":"\u0027FOLDER_ID\u0027 in parents and trashed = false","pageSize":1000,"fields":"files(id,name,mimeType,size,modifiedTime,webViewLink,md5Checksum),nextPageToken"}' --format json > /tmp/cts_drive_files.json
python scripts/download_drive.py --manifest /tmp/cts_drive_files.json --output /home/ubuntu/cts_data/pdfs --metadata-out /home/ubuntu/cts_data/metadata_by_local_name.json --workers 4
```

Lo scaricatore è riprendibile, verifica dimensione e MD5 di ogni PDF e registra le eccezioni in `download_errors.json`. Le copie scaricate e i metadati locali sono fuori dal repository. Se il download termina con errori, correggere o verificare quei file e rieseguirlo: i file già verificati non vengono scaricati di nuovo.

## Lettura integrale e indicizzazione

Dopo aver verificato il riepilogo del download, avviare una sola ingestione completa:

```bash
python scripts/ingest_local.py \
  --source /home/ubuntu/cts_data/pdfs \
  --metadata-json /home/ubuntu/cts_data/metadata_by_local_name.json
```

`--dry-run` esegue solo inventario e conteggio. Il manifesto completo, con almeno 433 ID Drive distinti, è obbligatorio ed è registrato nel database prima dei PDF. Prima dell'ingestione, lo script ricontrolla la dimensione e il checksum MD5 di ogni file. L'elaborazione è riprendibile per documento/pagina, preserva versioni e duplicati, associa citazioni alla pagina originale e aggiorna il registro degli errori. PyMuPDF estrae testo; le pagine senza testo utile passano a Tesseract in italiano/inglese. Il chunking E5 è basato sul tokenizer, include il prefisso entro il limite di 512 token e rifiuta input troppo lunghi invece di troncarli. I testi, gli hash, lo stato OCR e gli embedding vengono persistiti nel database. FAISS e TF-IDF sono indici volatili e ricostruibili dai dati persistenti. La consultazione viene abilitata solo quando ogni fonte attesa è verificata, tutte le pagine sono presenti e ogni pagina testuale ha embedding valido; pagine OCR vuote/errori restano bloccanti fino a revisione. Le query includono solo revisioni che corrispondono all'MD5 atteso e documenti canonici completi.

Per elencare fonti senza metadati, documenti incompleti e pagine OCR da rivedere, usare `python scripts/review_exceptions.py`. È possibile marcare come `verified-empty` soltanto una pagina senza testo con OCR `empty`, dopo aver controllato visivamente il PDF originale: `python scripts/review_exceptions.py --verify-empty DOCUMENT_ID PAGE --confirm-visual-review`. Gli errori OCR richiedono una nuova estrazione o un intervento, non possono essere dichiarati vuoti con quel comando.

I test della copertura e del filtro revisioni usano solo SQLite in memoria: `python -m unittest discover -s tests -v`. Non modificano il database gestito.

Gli originali restano in Drive. L'app mostra nel risultato il titolo, il numero/data disponibili (oppure dichiara che non sono identificati), la pagina e l'estratto; aprire il PDF originale può richiedere autorizzazioni Drive anche agli utenti presenti nella allowlist del sito.
