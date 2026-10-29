# Archivio riservato dei verbali CTS

Minisito in italiano con FastAPI, Jinja2 e MySQL gestito per interrogare i verbali CTS. La ricerca semantica usa FAISS-Cpu e `intfloat/multilingual-e5-small`; poiché il database gestito non supporta FULLTEXT, la ricerca lessicale usa scikit-learn TF-IDF in memoria, ricostruito da tutti i testi persistenti. Non è configurato alcun servizio vettoriale esterno.

## Stato attuale

La shell pubblica è online: [Archivio CTS](https://ctsrag-a2ilu96k.manus.space). La pagina di accesso è visibile ma il login non è attivato; la verifica anonima di `/api/status` ha restituito `401 Accesso richiesto.` senza esporre dati. È autorizzato il dominio esatto `lvogruppo.co`, già registrato nella allowlist; i sottodomini non sono inclusi. Le credenziali Resend e il mittente verificato non sono configurati e la copertura integrale è incompleta: la consultazione resta bloccata.

Tutti i 433 PDF (3.054.813.500 byte) sono stati scaricati e verificati con MD5. Il batch v2 è stato fermato alle 12:00; il v3 è stato fermato alle 13:16 per correggere problemi OCR, concorrenza e ripresa delle revisioni. Il batch v4 è stato interrotto dal limite di esecuzione di due ore alle 15:26, senza errore del parser. Snapshot MySQL in sola lettura alle 15:26: 433 fonti, 75 documenti processati con v4, 1 documento in `processing` (5/37 pagine), 5.682 chunk e 3.188 pagine `processed` più 27 `empty`; il gate resta chiuso. La nuova ripresa per pagina e la selezione di intervalli finiti sono state aggiunte e validate con 31 test, inclusi salto OCR prima della rasterizzazione e preservazione delle pagine `verified-empty`. Il batch successivo deve riprendere dall'indice 75; nessun risultato finale di copertura è ancora attestato.

## Avvio e variabili

Il runtime WebDev usa la porta 3000. In Sandbox, creare un ambiente virtuale e installare `requirements.txt`, poi avviare `uvicorn app.main:app --host 0.0.0.0 --port 3000`. Il database gestito è condiviso tra Preview e produzione: `create_schema()` crea tabelle mancanti e aggiunge solo colonne nullable, senza eliminare tabelle o dati.

L'app legge `DATABASE_URL` (in alternativa `DRIZZLE_DATABASE_URL`), `MANUS_API_URL` e `MANUS_API_KEY` dall'ambiente server. Non salvare i loro valori nel repository o nei log. Per l'accesso passwordless servono `RESEND_API_KEY` e un mittente verificato in `RESEND_FROM_EMAIL`; non configurati al momento. Sono supportati indirizzi singoli in `cts_allowed_emails` e domini esatti in `cts_allowed_domains`; i sottodomini non sono inclusi automaticamente. Per aggiungere un account dopo l'autorizzazione: `python scripts/manage_allowlist.py --add utente@dominio.it`. Per aggiungere un dominio esatto autorizzato: `python scripts/manage_allowlist.py --add-domain lvogruppo.co`.

Il codice OTP scade dopo 10 minuti, ha un massimo di 5 tentativi e non viene inviato a indirizzi non autorizzati. Il rate limit è applicato prima del controllo allowlist e la risposta della richiesta codice è uguale per indirizzi ammessi e non ammessi, per ridurre l'enumerazione.

La cookie di sessione usa `HttpOnly` e, su Preview HTTPS, `SameSite=None; Secure`. Per una prova HTTP locale diretta si può impostare esplicitamente `APP_COOKIE_SECURE=false`; non usare questo valore per Preview o pubblicazione.

## Inventario e download da Drive

L'audit iniziale ha identificato 433 PDF per circa 3,05 GB. Il download usa la CLI Google Workspace già autenticata e lascia gli originali nella cartella Drive. Aggiornare prima l'inventario (sostituire `FOLDER_ID` con l'ID della cartella autorizzata):

```bash
gws drive files list --params '{"q":"\u0027FOLDER_ID\u0027 in parents and trashed = false","pageSize":1000,"fields":"files(id,name,mimeType,size,modifiedTime,webViewLink,md5Checksum),nextPageToken"}' --format json > /tmp/cts_drive_files.json
python scripts/download_drive.py --manifest /tmp/cts_drive_files.json --output /home/ubuntu/cts_data/pdfs --metadata-out /home/ubuntu/cts_data/metadata_by_local_name.json --workers 4
```

Lo scaricatore è riprendibile, verifica dimensione e MD5 di ogni PDF e registra le eccezioni in `download_errors.json`. Le copie scaricate e i metadati locali sono fuori dal repository. Se il download termina con errori, correggere o verificare quei file e rieseguirlo: i file già verificati non vengono scaricati di nuovo.

## Lettura integrale e indicizzazione

Dopo aver verificato il riepilogo del download, avviare l'ingestione completa. Per lotti entro il limite di durata del Sandbox, usare `--start-index` (inclusivo) e `--max-files`; il manifesto e i checksum restano completi in ogni esecuzione:

```bash
python scripts/ingest_local.py \
  --source /home/ubuntu/cts_data/pdfs \
  --metadata-json /home/ubuntu/cts_data/metadata_by_local_name.json
```

Esempio di intervallo da 50 file: aggiungere `--start-index 0 --max-files 50`; la successiva esecuzione usa l'indice successivo. I PDF verificati vengono ordinati stabilmente per nome.

`--dry-run` esegue solo inventario e conteggio. Il manifesto completo, con almeno 433 ID Drive distinti, è obbligatorio ed è registrato nel database prima dei PDF. Prima dell'ingestione, lo script ricontrolla dimensione e checksum MD5 di ogni file. L'elaborazione v4 è riprendibile pagina per pagina e serializzata da un lock advisory MySQL; risolve l'ID di revisione prima del fast-path e aggiorna i metadati a parità di MD5. Nella stessa versione conserva le pagine con stato finale e conteggio chunk esatto, saltandole prima di estrazione/OCR/rasterizzazione, e ricostruisce solo pagine incomplete; una nuova versione o un diverso numero di pagine richiede la reindicizzazione completa del documento. La CLI supporta intervalli deterministici con `--start-index`/`--max-files` senza ridurre il manifesto né i controlli checksum. PyMuPDF estrae testo pagina per pagina; tutte le pagine con immagine che copre almeno il 45% vengono sottoposte a OCR italiano/inglese anche se il layer nativo appare plausibile. L'OCR è accettato solo con almeno 20 parole alfanumeriche, 120 caratteri alfanumerici, 12 parole per megapixel, copertura verticale del testo di almeno il 20% e mediana confidence di almeno 50; mismatch sostanziale col layer nativo, timeout, errore o densità/copertura insufficiente restano eccezioni bloccanti, non testo completo. L'OCR è limitato a 16 milioni di pixel e 120 secondi per pagina. Il chunking E5 usa generatori token/offset a finestre sovrapposte e persiste massimo 64 chunk per lotto; ogni pagina resta `processing` durante i commit e viene promossa solo dopo aver salvato il conteggio esatto dei chunk. Il gate confronta tale conteggio con le righe persistite e respinge pagine incomplete. Gli embedding non finiti o con dimensione/byte non conformi tengono chiusa la copertura; il fingerprint degli embedding invalida FAISS anche per modifiche in-place. I testi, gli hash, lo stato OCR, il conteggio chunk e gli embedding sono persistiti nel database. FAISS e TF-IDF sono ricostruibili dai dati persistenti. Le query includono solo revisioni corrispondenti all'MD5 atteso e documenti canonici completi. Il gate blocca la consultazione finché ogni fonte e ogni pagina non risultano verificate.

Per elencare fonti senza metadati, documenti incompleti e pagine OCR da rivedere, usare `python scripts/review_exceptions.py`. È possibile marcare come `verified-empty` soltanto una pagina senza testo con OCR `empty`, dopo aver controllato visivamente il PDF originale: `python scripts/review_exceptions.py --verify-empty DOCUMENT_ID PAGE --confirm-visual-review`. Gli errori OCR richiedono una nuova estrazione o un intervento, non possono essere dichiarati vuoti con quel comando.

I 31 test di copertura, fingerprint, OCR, metadati, lock, ripresa pagina-per-pagina/revisione, selezione batch e persistenza a lotti usano solo SQLite in memoria: `python -m unittest discover -s tests -v`. Non modificano il database gestito.

Gli originali restano in Drive. L'app mostra nel risultato il titolo, il numero/data disponibili (oppure dichiara che non sono identificati), la pagina e l'estratto; aprire il PDF originale può richiedere autorizzazioni Drive anche agli utenti presenti nella allowlist del sito.
