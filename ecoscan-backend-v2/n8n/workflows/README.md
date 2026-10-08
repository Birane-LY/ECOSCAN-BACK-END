# Workflow n8n EcoScan — opportunités de financement

Le fichier `ecoscan-opportunities-ingestion.json` est un workflow importable dans n8n. Il consulte les pages sources, suit les liens candidats vers les portails externes en HTTPS, fait détecter et extraire les offres par le modèle configuré, valide les données puis envoie chaque offre valide à Django. Les IP littérales, hôtes locaux et ports non standard sont écartés ; chaque candidat est traité séquentiellement.

## Variables d’environnement n8n requises

Configurez ces variables sur l’instance n8n avant d’activer le workflow :

- `GROQ_API_KEY` : clé API Groq, à garder secrète.
- `ECOSCAN_LLM_ENDPOINT` : facultative ; par défaut `https://api.groq.com/openai/v1/chat/completions`.
- `ECOSCAN_FAST_MODEL` : modèle de détection facultatif ; par défaut `openai/gpt-oss-20b`.
- `ECOSCAN_EXTRACTION_MODEL` : modèle d’extraction facultatif ; par défaut `openai/gpt-oss-20b`.
- `ECOSCAN_INGESTION_URL` : URL absolue de l’endpoint Django `/api/analyses/opportunites-financement/ingestion/`.
- `N8N_INGESTION_TOKEN` : doit correspondre exactement au `N8N_INGESTION_TOKEN` configuré dans Django.

Ne placez aucune clé API ni aucun jeton dans le fichier JSON exporté. En local, vérifiez que l’URL Django est joignable depuis le processus ou conteneur n8n : dans un conteneur, `localhost` désigne ce conteneur, pas la machine hôte.

Le workflow lit ces variables avec `$env`. Le fichier Compose fourni active `N8N_BLOCK_ENV_ACCESS_IN_NODE=false` pour autoriser cet accès. C’est adapté à une instance locale de confiance ; sur une instance partagée, préférez des credentials n8n dédiés et ne donnez pas l’accès à des personnes non fiables.

## Import et test

1. Importez le fichier JSON dans n8n.
2. Configurez les variables d’environnement ci-dessus et redémarrez n8n si votre installation l’exige.
3. Vérifiez que Django possède le même `N8N_INGESTION_TOKEN` et qu’il est accessible depuis n8n.
4. Lancez **Lancement manuel** et inspectez l’exécution. Les sources indisponibles sont ignorées ; les erreurs du modèle ou de l’envoi à Django restent visibles comme échecs.
5. Vérifiez que la réponse de l’endpoint indique des enregistrements créés ou mis à jour, puis activez **Collecte quotidienne**.

Chaque offre est envoyée individuellement à l’endpoint d’ingestion, qui effectue une mise à jour ou création par titre et organisme. Django publie les offres dont la confiance d’extraction atteint le seuil configuré ; les autres attendent la validation d’un administrateur avant d’apparaître au catalogue.

## Workflow de notification des anomalies

Le fichier `ecoscan-anomaly-notification.json` reçoit les événements Django
`POST /webhook/anomalie-detectee`, vérifie le jeton d'authentification,
valide les adresses des administrateurs puis envoie un e-mail texte aux
destinataires de l'organisation. Le webhook ne répond avec succès qu'après
l'envoi SMTP ; une erreur de validation ou d'envoi reste visible comme échec
d'exécution dans n8n.

### Configuration

1. Dans `n8n/.env`, configurez `N8N_WEBHOOK_TOKEN` avec la même valeur que
   `N8N_WEBHOOK_TOKEN` dans Django, et définissez
   `ECOSCAN_NOTIFICATION_FROM_EMAIL` sur une adresse expéditrice vérifiée.
   Ne réutilisez pas le jeton d'ingestion des opportunités.
2. Redémarrez n8n avec `docker compose up -d` pour charger les variables
   d'environnement.
3. Importez `ecoscan-anomaly-notification.json` et sélectionnez un credential
   SMTP valide dans le nœud **Envoyer l'alerte par e-mail**.
4. Activez le workflow. Django doit avoir
   `N8N_WEBHOOK_BASE_URL=https://<n8n-host>/webhook` et
   `BACKEND_BASE_URL=https://<backend-host>`.
5. Lancez l'analyse des anomalies sur un jour de test et vérifiez l'exécution
   n8n ainsi que la réception de l'e-mail. Les exécutions actives ne renvoient
   pas de notification ; une anomalie résolue puis détectée à nouveau envoie
   un nouvel événement.

Ne placez aucun vrai jeton ni credential SMTP dans le fichier JSON, `.env.example`
ou Git. Le workflow emploie les variables d'environnement uniquement pour le
jeton et l'adresse expéditrice ; le credential SMTP se configure dans n8n.
