# Piano di implementazione — Archivio riservato dei verbali CTS

## Obiettivo e decisioni

Realizzare un minisito in italiano, ad accesso riservato, per interrogare in modo verificabile i verbali del Comitato Tecnico Scientifico. L'audit iniziale ha individuato 433 PDF (circa 3,05 GB), con varianti e copie da riconciliare; non si useranno campioni per dichiarare copertura completa. Tutti i 433 PDF sono stati scaricati e verificati con MD5; la pipeline ricontrolla dimensione e checksum prima di ogni importazione. Dopo una pausa di sicurezza e la conferma esplicita dell'utente, l'ingestione token-safe è stata riavviata in modo idempotente; il batch precedente si era fermato dopo quattro PDF, quindi la copertura integrale non è ancora attestata.

Su delega dell'utente scelgo una soluzione senza un nuovo servizio vettoriale a pagamento: **database MySQL gestito già abilitato + FAISS-Cpu nel backend + modello Hugging Face `intfloat/multilingual-e5-small`**. FAISS è una libreria MIT, non un database né un servizio ospitato. MySQL rimane la fonte persistente di documenti, testo estratto, metadati e vettori; all'avvio il backend ricostruisce in memoria l'indice FAISS dagli embedding salvati.

Il database gestito di questo progetto non supporta indici FULLTEXT. Per non introdurre un servizio esterno o un nuovo costo ricorrente, l'indice lessicale sarà **TF-IDF con scikit-learn, ricostruibile in memoria da tutti i chunk testuali persistenti**. Il punteggio lessicale TF-IDF e la ricerca vettoriale esatta di FAISS saranno combinati con Reciprocal Rank Fusion. Ogni ricerca confronta l'intero insieme indicizzato; le citazioni mostrate all'utente restano una selezione dei passaggi più pertinenti, quindi l'interfaccia non dichiarerà esaustiva una risposta se la copertura documentale è incompleta.

Il modello E5-small ha embedding da 384 dimensioni, supporta italiano e retrieval multilingue, ha licenza MIT e accetta fino a 512 token; per il retrieval si useranno i prefissi `query:` e `passage:`. Il chunking usa token e offset del tokenizer (460 token di contenuto, sovrapposizione di 64), verifica il prefisso e rifiuta ogni input che superi 512 token: non è ammesso troncamento silenzioso. La versione di chunking è registrata per forzare il re-index dei dati creati con la prima passata. I pesi saranno eseguiti nel backend, senza chiamate di inferenza a Hugging Face. Il modello sarà valutato sui verbali; se la qualità non basta, la sostituzione con BGE-M3 sarà una scelta successiva basata su un confronto, non un presupposto.

Il backend userà l'API LLM gestita da Manus solo per formulare risposte a partire dai passaggi recuperati; una chiamata di prova ha confermato servizio e citazioni. Le risposte dovranno distinguere fatti citati, sintesi e inferenze; non dovranno colmare lacune inventando dati. Un quesito comparativo valuta l'intero indice e amplia il contesto fino a 40 passaggi, con non più di due passaggi per documento; la UI chiarisce che i passaggi mostrati non provano da soli l'esaustività. Il retrieval filtra in aggiunta alle revisioni correnti verificate nel manifesto e non sarà presentato come prova di copertura se sono rimasti documenti o pagine non indicizzati o OCR da verificare.

Gli originali PDF resteranno in Drive per non duplicare nel sito l'intero archivio. MySQL conserverà i metadati, il testo estratto per pagina, i chunk, gli embedding e gli esiti di elaborazione necessari alle query. Le citazioni mostreranno verbale, data, pagina ed estratto direttamente nel minisito; numero o data non identificati saranno dichiarati esplicitamente senza inferenze. Il collegamento al PDF originale potrà richiedere agli utenti anche l'autorizzazione Drive.

## Funzioni

- Ricerca lessicale TF-IDF e semantica FAISS sull'intero indice, con filtri per data e numero del verbale; la ricerca esclude revisioni non correnti e fonti non verificate.
- Consultazione in linguaggio naturale con riferimenti verificabili a verbale, data, pagina ed estratto.
- Lettura integrale dei PDF accessibili, pagina per pagina, con estrazione testuale e OCR Tesseract per le scansioni quando necessario.
- Inventario con nomi, hash, date, varianti/duplicati, stato di ogni pagina e registro delle eccezioni; copie identiche potranno essere deduplicate nell'indice senza cancellare la provenienza.
- Accesso passwordless via email con codice monouso, consegnato tramite Resend, e allowlist privata che sarà fornita dall'utente. Niente Google Sign-In, password o registrazione pubblica. Una allowlist vuota deve negare l'accesso a tutti. Finché mancano la chiave Resend, il mittente verificato e gli account autorizzati, la pagina di accesso non abilita le funzioni di consultazione.

## Implementazione e integrazioni

Una singola applicazione FastAPI/Python servirà pagine Jinja2, CSS e JavaScript essenziale, API di ricerca/query e `/health`; non si introdurrà un frontend Node separato. SQLAlchemy creerà in modo additivo le tabelle per inventario atteso, verbali, pagine, chunk, vettori, allowlist, codici OTP e sessioni. Il test di connessione TLS è riuscito. L'ingestione richiede il manifesto completo Drive (almeno 433 ID distinti), ricontrolla MD5 e dimensione e memorizza ogni fonte attesa prima di processare i PDF; il gate confronta l'inventario con i documenti e verifica conteggio/continuità pagine, stato OCR, versione di chunking e presenza di embedding per ogni pagina testuale. Il gate passa agli endpoint l'insieme dei soli documenti canonici completi e corrispondenti all'MD5 atteso; le revisioni obsolete non possono entrare nei risultati. FAISS calcolerà la similarità coseno esatta su tutti i vettori in memoria e TF-IDF la similarità lessicale su tutti i chunk testuali; una fusione rank-based combinerà le liste prima della generazione.

PyMuPDF estrarrà testo e numero di pagina. Pagine con meno di 80 caratteri utili, o con meno di 400 caratteri e immagini che coprono almeno il 45% della pagina, saranno candidate all'OCR con Tesseract in italiano/inglese; i fallimenti, le pagine vuote e le letture incerte saranno conservati come eccezioni verificabili. Ogni record manterrà file ID/URL Drive, hash, variante, numero pagina e stato OCR, così che l'elaborazione possa essere ripresa e non riparta indiscriminatamente a ogni avvio.

L'autenticazione applicativa userà codici OTP monouso con scadenza, consumo transazionale atomico e verifica server-side della allowlist. I segreti Resend non verranno inseriti in Git o nel client. I cookie di sessione saranno HttpOnly, Secure e `SameSite=None` su Preview HTTPS incorporata, con gestione separata per eventuale HTTP locale. Per richiesta esplicita dell'utente è stato pubblicato il solo guscio pubblico bloccato: [Archivio CTS](https://ctsrag-a2ilu96k.manus.space) (versione `bb21284c01c9f7e380513ac5ad341aa818e6c7b1`). La pagina di accesso è raggiungibile, il login non è attivato e la chiamata anonima a `/api/status` restituisce 401; non vengono esposti verbali, citazioni, stato documentale o API RAG. Il gate server-side blocca anche le sessioni esistenti finché Resend, allowlist e copertura integrale verificata non sono tutti pronti.

## Struttura del progetto

- `app/main.py`: pagine, API private, health check e protezione delle sessioni.
- `app/security.py`: OTP, allowlist, sessioni opache e invio tramite Resend.
- `app/database.py`: connessione MySQL/TLS, modelli SQLAlchemy e creazione additiva dello schema.
- `app/retrieval/embeddings.py`: caricamento e codifica E5-small con prefissi query/passage.
- `app/retrieval/index.py`: ricostruzione in memoria dell'indice vettoriale FAISS dai vettori persistenti.
- `app/retrieval/lexical.py`: ricostruzione TF-IDF dall'intero insieme di chunk testuali.
- `app/retrieval/search.py`: filtri, ricerca ibrida limitata ai documenti eleggibili e fusione dei risultati con metadati di pagina.
- `app/services/ingest.py`: estrazione PDF/OCR e chunking token-aware con pagina, offset e hash.
- `app/services/coverage.py`: copertura verificabile e insieme dei documenti canonici correnti ammessi al retrieval.
- `app/services/answers.py`: chiamate al modello LLM gestito e vincoli di citazione/astensione.
- `app/templates/`: login e consultazione riservata.
- `app/static/`: CSS e JavaScript essenziale dell'interfaccia.
- `scripts/download_drive.py`: download riprendibile dei PDF della cartella Drive con verifica di dimensione/checksum e salvataggio dei metadati, fuori dal repository.
- `scripts/ingest_local.py`: ingestione riprendibile con manifesto completo obbligatorio, ricontrollo MD5, inventario atteso persistente, OCR, re-index per versione e deduplicazione per hash.
- `scripts/review_exceptions.py`: report locale di fonti, metadati e pagine incomplete; consente di attestare una pagina vuota solo dopo revisione visiva esplicita.
- `scripts/manage_allowlist.py`: aggiunta amministrativa degli account autorizzati senza endpoint pubblico di registrazione.
- `tests/test_archive_coverage.py`: test isolati su SQLite in memoria per copertura e filtro delle revisioni, senza scritture sul MySQL gestito.
- `public/manus-routes.json`: manifest statico delle pagine.
- `Dockerfile`, `requirements.txt`, `pyrightconfig.json`: runtime, dipendenze e diagnostica.

## Direzione visiva

**Design movement:** sala di lettura archivistica contemporanea, con rigore editoriale svizzero e richiami sobri ai registri cartacei. **Principi:** leggibilità documentale, provenienza sempre visibile, densità senza affollamento, interazioni calme e reversibili. **Color philosophy:** fondo avorio da carta, inchiostro blu-notte e verde archivio come colore proprietario; terracotta solo per evidenziare note o anomalie. **Layout:** barra laterale stretta per archivio e filtri, area principale di ricerca e risultati; niente griglia promozionale centrata. **Signature elements:** numeri di pagina a margine, sottili filetti da schedario, chip-citazione con numero/data/pagina. **Interaction:** ogni risultato porta subito alla prova documentale; filtri e query restano modificabili, nessun effetto nasconde lo stato della ricerca. **Animation:** transizioni discrete di 120–180 ms per mostrare risultati; nessuna animazione decorativa continua. **Typography:** IBM Plex Sans per controlli/UI e Source Serif 4 per estratti e lettura; gerarchia tipografica compatta e ad alto contrasto. **Brand essence:** “I verbali CTS, interrogabili e verificabili per chi è autorizzato”; personalità precisa, sobria, affidabile. **Brand voice:** chiara e documentale; esempi: “Trova il passaggio nei verbali” e “Verifica la fonte: verbale, data, pagina”. **Wordmark/logo:** sigla CTS con tre fogli sovrapposti e un piccolo segnalibro di pagina. **Signature brand color:** verde archivio scuro, usato per azioni e stato verificato.
