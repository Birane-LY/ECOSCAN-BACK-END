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

1. Depuis `ecoscan-backend-v2\n8n`, configurez `.env.local` à partir de
   `.env.example` sans écraser un fichier local existant.
   Ne laissez pas les valeurs `replace-with-*`. Le jeton
   `N8N_WEBHOOK_TOKEN` doit être identique dans `n8n/.env.local` et le `.env` Django ;
   `N8N_INGESTION_TOKEN` est un jeton distinct, identique côté Django et n8n.
   Si `N8N_INGESTION_TOKEN` est déjà configuré dans le `.env` Django, gardez
   cette valeur : reportez-la dans `n8n/.env.local` localement, sans la remplacer ni
   la partager.
2. Créez au préalable le réseau `ecoscan-automation` s'il n'existe pas, puis
   depuis `n8n`, validez et démarrez avec
   `docker compose --env-file .env.local config --quiet` et
   `docker compose --env-file .env.local up -d`. Consultez l'état avec
   `docker compose --env-file .env.local ps` et attendez `healthy`.
   L'interface locale est
   disponible sur `http://localhost:5678`.
3. Importez le fichier JSON dans n8n (ou utilisez les exports montés en lecture
   seule sous `/opt/ecoscan/workflows` dans le conteneur). L'import ne rend pas
   le workflow actif.
4. Vérifiez que Django possède le même `N8N_INGESTION_TOKEN` et qu’il est
   accessible depuis n8n. Depuis un environnement de test, lancez
   **Lancement manuel** et inspectez l’exécution. Ce workflow appelle des sites
   externes, Groq et l'endpoint Django, et peut créer ou modifier des
   opportunités : ne lancez pas ce test contre des données de production sans
   l'avoir explicitement décidé.
5. Après validation du résultat de test, activez **Collecte quotidienne**.

Chaque offre est envoyée individuellement à l’endpoint d’ingestion, qui effectue une mise à jour ou création par titre et organisme. Django publie les offres dont la confiance d’extraction atteint le seuil configuré ; les autres attendent la validation d’un administrateur avant d’apparaître au catalogue.

## Workflow de notification des anomalies

Le fichier `ecoscan-anomaly-notification.json` reçoit les événements Django
`POST /webhook/anomalie-detectee`, vérifie le jeton d'authentification,
valide les adresses des administrateurs puis envoie un e-mail texte aux
destinataires de l'organisation. Le webhook ne répond avec succès qu'après
l'envoi SMTP ; une erreur de validation ou d'envoi reste visible comme échec
d'exécution dans n8n.

### Configuration

1. Dans `n8n/.env.local`, configurez `N8N_WEBHOOK_TOKEN` avec la même valeur que
   `N8N_WEBHOOK_TOKEN` dans Django, et définissez
   `ECOSCAN_NOTIFICATION_FROM_EMAIL` sur une adresse expéditrice vérifiée.
   Ne réutilisez pas le jeton d'ingestion des opportunités.
2. Redémarrez n8n avec `docker compose --env-file .env.local up -d` pour charger les variables
   d'environnement.
3. Importez `ecoscan-anomaly-notification.json` et sélectionnez un credential
   SMTP valide dans le nœud **Envoyer l'alerte par e-mail**. Sans ce credential,
   l'envoi échoue et n8n ne doit pas être considéré comme opérationnel.
4. Activez le workflow après avoir confirmé la configuration SMTP. Django doit
   avoir `N8N_WEBHOOK_BASE_URL=https://<n8n-host>/webhook` et
   `BACKEND_BASE_URL=https://<backend-host>`.
5. Vérifiez le webhook et la réception du courriel uniquement sur un environnement
   de test avec une adresse de test. Une analyse réelle peut envoyer un courriel
   aux administrateurs et n'est pas un test sans effet de bord.

## Alertes de suivi énergétique

`ecoscan-energy-alert-notification.json` reçoit les événements Django
`POST /webhook/alerte-energetique`, vérifie `X-N8N-Webhook-Token`, valide les
destinataires administrateurs actifs et envoie un e-mail. Il couvre les alertes
de solde et d'autonomie Woyofal, d'approche d'une tranche, d'écart facture-
monitoring et de progression des objectifs. Les événements incluent leur
provenance et distinguent les scénarios simulés des mesures réelles. Les alertes
d'approche de tranche ne sont actuellement pas émises : le backend attend la
confirmation des périodes de référence et des règles de remise à zéro.

Configurez le même `N8N_WEBHOOK_TOKEN` côté Django et n8n, ainsi que
`ECOSCAN_NOTIFICATION_FROM_EMAIL` et un credential SMTP dans n8n. Importez le
workflow, vérifiez son exécution sur un environnement de test, puis activez-le
uniquement après configuration du transport d'e-mail. Le workflow est exporté
inactif. La routine Django quotidienne évalue ces alertes; une clé
d'idempotence enregistrée dans le journal d'audit évite les doublons et les
échecs de notification restent réessayables.

Les barèmes actifs sont déterminés par la catégorie (`DPP`, `DMP`, `PPP`, `PMP`)
et le mode (`PREPAYE`, `POSTPAYE`), avec les tarifs officiels au 1er janvier
2026. En prépaiement, le prix de la troisième tranche est celui de la deuxième.
Les seuils de tranche ne sont pas calculés tant que la période de référence et
les règles de remise à zéro ne sont pas confirmées. Un montant de recharge seul
ne permet pas d'estimer les kWh crédités sans les retenues du ticket. La
comparaison d'une facture exige sa période extraite complète, un compteur
attribué, une couverture temporelle suffisante et un seul capteur ENERGY
applicable; aucun total n'est inventé pour un site sans capteur unique.

## Audit nocturne des climatiseurs (lecture seule)

`ecoscan-nightly-climate-audit.json` lit uniquement les états d'une liste
explicite d'entités Home Assistant chaque jour à 22 h 45 et envoie un rapport
par e-mail. Il accepte les véritables entités `climate.*` et les entités de
démonstration `input_boolean.ecoscan_demo_climatisation_*`. Ces dernières sont
étiquetées comme simulation dans le rapport et ne prouvent pas l'état des
climatiseurs physiques. Le workflow n'appelle aucun service Home Assistant et
**n'envoie aucune commande aux équipements**.

Configurez dans `n8n/.env.local` :

- `HOME_ASSISTANT_URL` : URL joignable depuis le conteneur n8n.
- `HOME_ASSISTANT_TOKEN` : jeton d'accès Home Assistant, gardé privé.
- `ECOSCAN_CLIMATE_ENTITY_IDS` : liste séparée par des virgules des seules
  entités autorisées, par exemple `climate.bureau` ou
  `input_boolean.ecoscan_demo_climatisation_open_space`. Les interrupteurs de
  démonstration `input_boolean` représentent uniquement des états simulés.
  Utilisez une seule famille d'entités par liste; le workflow refuse de
  mélanger les données simulées et les états `climate` réels.
- `ECOSCAN_ENERGY_REPORT_TO_EMAIL` : adresse de la personne responsable.
- `ECOSCAN_NOTIFICATION_FROM_EMAIL` et le credential SMTP dans n8n.

Importez le workflow, associez le credential SMTP et vérifiez manuellement
l'exécution avant de l'activer. Le fuseau horaire suit `TZ` de n8n. Si une
entité ne peut pas être lue, l'exécution échoue visiblement plutôt que de
conclure qu'elle est arrêtée.

## Relances de facturation par e-mail

`ecoscan-billing-reminder.json` reçoit le webhook Django
`/webhook/ecoscan-relance`, vérifie `X-EcoScan-Secret`, puis envoie une relance
uniquement lorsque le canal demandé est `EMAIL` et que les adresses des
administrateurs sont valides. Après l'envoi ou un échec SMTP, il rappelle le
callback Django avec le même secret pour marquer la relance `SENT` ou `FAILED`.
Les canaux SMS, WhatsApp et notification interne ne sont pas implémentés par
ce workflow et sont remontés comme échec au backend.

Configurez `N8N_SHARED_SECRET` dans `n8n/.env.local` et le `.env` Django avec la même
valeur forte, puis configurez Django :

```dotenv
N8N_RELANCE_WEBHOOK_URL=http://ecoscan-n8n:5678/webhook/ecoscan-relance
```

Dans `n8n/.env.local`, `BACKEND_BASE_URL` doit être `http://backend:8000` pour
le callback vers Django sur le réseau Docker partagé. Dans une autre
topologie, adaptez les URL afin que Django joigne n8n et que le conteneur n8n
joigne Django. Redémarrez les deux services après les changements avec leurs
Compose et fichiers d'environnement respectifs.
Importez le workflow, associez le credential SMTP, testez en staging avec une
facture et des destinataires de test, puis activez-le. Une relance réelle envoie
un e-mail et modifie son statut de facturation : ne la testez pas sur des
données réelles sans l'avoir explicitement décidé.

## Limites et ordre de mise en service

Les exports couvrent la collecte d'opportunités, les alertes d'anomalie, l'audit
nocturne en lecture seule et les relances par e-mail. Le jeton
`N8N_INGESTION_TOKEN` déjà présent dans votre `.env` est celui de la collecte
d'opportunités ; conservez-le tel quel côté Django et n8n, sans le copier dans
un export ni le partager. Les autres flux utilisent des secrets distincts.

Le code contient un client pour `/onboarding-cree`, mais aucun appel actuel à ce
client n'a été trouvé : un workflow d'e-mail d'onboarding ne recevrait donc pas
d'événement. Le pilote autonome Django reste explicitement simulé ; la passerelle
actuelle n'autorise pas le domaine Home Assistant `climate`. Pour cette raison,
aucun workflow ne coupe automatiquement les climatiseurs. Cette évolution doit
attendre un pilote matériel séparé avec règles d'occupation, exceptions,
criticité, confirmation d'état et arrêt d'urgence.

Ne placez aucun vrai jeton ni credential SMTP dans le fichier JSON, `.env.example`
ou Git. Le workflow emploie les variables d'environnement uniquement pour le
jeton et l'adresse expéditrice ; le credential SMTP se configure dans n8n.
