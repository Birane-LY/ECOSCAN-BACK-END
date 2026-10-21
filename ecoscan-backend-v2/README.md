# EcoScan Backend V2

API et services métier de la plateforme EcoScan : organisations et sites,
consommation énergétique, factures et compteurs, équipements et capteurs,
analyses, recommandations, notifications, abonnements et paiement.

Ce dépôt contient le backend Django V2 et les composants de développement qui
l’accompagnent : simulateur de télémétrie, tâches planifiées, passerelle locale
Home Assistant, configuration Home Assistant de développement et exports de
workflows n8n.

> **Important — simulation et matériel réel**
>
> Les données de démonstration, capteurs `SIMULATED` et `input_boolean` Home
> Assistant sont des simulations. Ils ne constituent ni une mesure électrique
> réelle ni la preuve de l’état physique d’un appareil. La passerelle peut
> envoyer de véritables commandes ON/OFF aux entités qu’on lui associe :
> n’utilisez jamais son mapping d’exemple sur du matériel réel.

## Sommaire

- [Architecture et répertoires](#architecture-et-répertoires)
- [Prérequis](#prérequis)
- [Installation locale sous Windows](#installation-locale-sous-windows)
- [Configuration Django](#configuration-django)
- [Démarrage et migrations](#démarrage-et-migrations)
- [Lancement avec Docker Compose](#lancement-avec-docker-compose)
- [Applications et API](#applications-et-api)
- [Capteurs, simulation et monitoring](#capteurs-simulation-et-monitoring)
- [Analyses et traitements planifiés](#analyses-et-traitements-planifiés)
- [Facturation et paiements](#facturation-et-paiements)
- [Automatisations n8n](#automatisations-n8n)
- [Passerelle Home Assistant](#passerelle-home-assistant)
- [Tests et contrôles](#tests-et-contrôles)
- [Fichiers, données et sauvegardes](#fichiers-données-et-sauvegardes)
- [Sécurité et mise en production](#sécurité-et-mise-en-production)
- [Dépannage](#dépannage)
- [Documentation complémentaire](#documentation-complémentaire)

## Architecture et répertoires

EcoScan Backend V2 utilise Django et Django REST Framework. Les applications
principales sont :

| Application | Responsabilité |
| --- | --- |
| `accounts` | Comptes, authentification, organisations de l’utilisateur et intégrations internes. |
| `organizations` | Organisations, membres, sites et compteurs. |
| `energy` | Données énergétiques, factures, objectifs, tarifs et prévisions. |
| `energy_assets` | Équipements, capteurs, mesures, monitoring, anomalies, occupation et commandes. |
| `analysis` | Analyses, contexte, rapports, recommandations et alertes énergétiques. |
| `billing` | Plans, abonnements, facturation, relances et intégration PayDunya. |
| `audit` | Journalisation des événements et opérations métier. |

Les points d’entrée HTTP sont déclarés dans `config/urls.py` :

- `/admin/` : interface d’administration Django.
- `/api/` : comptes et authentification, ainsi que les routes de l’application `accounts`.
- `/api/organisations/` : organisations, membres, sites et compteurs.
- `/api/energies/` : données et fonctionnalités énergétiques.
- `/api/energy-assets/` : équipements, capteurs, mesures, anomalies, monitoring et occupation.
- `/api/analyses/` : analyses, rapports, recommandations et opportunités.
- `/api/audits/` : consultations du journal d’audit.
- `/api/billing/` et `/api/paiements/` : routes de facturation et compatibilité de paiement.
- `/api/internal/` : routes de service à service; elles doivent rester sur un réseau privé.

Le backend utilise des JWT pour l’API. Les permissions REST par défaut
requièrent une authentification; certaines routes publiques ou internes
définissent explicitement leur propre politique.

## Prérequis

### Développement Python natif

- Windows 10/11 ou un système compatible avec Python.
- Python 3.12 recommandé (la version Docker est `python:3.12-slim`).
- `pip` et un environnement virtuel Python.
- PostgreSQL 14 ou plus récent si vous choisissez PostgreSQL en local.
- Tesseract OCR avec les données linguistiques françaises et Poppler si vous
  exécutez le traitement des factures PDF/images hors Docker.

Les dépendances Python sont déclarées dans `requirements.txt`. Elles incluent
Django, Django REST Framework, Simple JWT, psycopg, Pillow, pytesseract,
pdf2image, ReportLab, httpx et les outils de configuration de base de données.

### Docker

- Docker Desktop démarré (ou Docker Engine et Docker Compose V2).
- Accès aux images Docker nécessaires.
- Pour le Compose principal, le dépôt du service IA doit aussi être disponible
  au chemin relatif `..\..\ECOSCAN IA` depuis `ecoscan-backend-v2`, avec son
  Dockerfile et son fichier d’environnement local. Adaptez le Compose si votre
  arborescence diffère.

## Installation locale sous Windows

Depuis PowerShell, placez-vous dans le répertoire du backend :

```powershell
Set-Location 'C:\chemin\vers\ECOSCAN BACK-END\ecoscan-backend-v2'
python --version
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
Copy-Item .env.example .env
```

Si PowerShell interdit l’activation du script dans la session courante, vous
pouvez appeler directement `.\.venv\Scripts\python.exe` et
`.\.venv\Scripts\pip.exe` sans changer la stratégie d’exécution globale.

Ouvrez ensuite `.env` dans un éditeur local et configurez les variables
nécessaires à votre environnement. Ne partagez pas ce fichier et ne le
committez jamais. Pour un démarrage local rapide avec SQLite, sélectionnez
`DB_BACKEND=sqlite` et utilisez `DJANGO_DEBUG=1`. Pour PostgreSQL, configurez
la base et ses identifiants; le code de développement peut retomber sur SQLite
si PostgreSQL est indisponible et `DB_ALLOW_SQLITE_FALLBACK=True`.

Préparez le schéma et un compte administrateur :

```powershell
python manage.py check
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver 127.0.0.1:8000
```

L’API locale est alors disponible à `http://127.0.0.1:8000/`. L’interface
d’administration se trouve à `http://127.0.0.1:8000/admin/`. Le serveur de
développement Django ne doit pas être exposé sur Internet.

### Traitement local de factures

Le conteneur Docker installe `tesseract-ocr`, les données françaises et
`poppler-utils`. En installation native, installez également Tesseract avec le
modèle de langue `fra` et Poppler, puis assurez-vous que leurs exécutables sont
accessibles dans le `PATH`. Les dépendances Python seules ne fournissent pas
ces binaires système.

## Configuration Django

`.env.example` documente les variables d’environnement. Copiez-le vers `.env`
et remplacez uniquement les paramètres nécessaires; les exemples ne sont pas
des identifiants fonctionnels.

### Django, réseau et sécurité

| Variable | Rôle |
| --- | --- |
| `DJANGO_SECRET_KEY` | Clé secrète Django, unique et aléatoire par environnement. |
| `DJANGO_DEBUG` | Active les réglages de développement. Doit être désactivé en production. |
| `DJANGO_ALLOWED_HOSTS` | Noms/IP autorisés pour l’en-tête `Host`, séparés par des virgules. |
| `FRONTEND_URL` | Origine du front-office pour les liens d’activation. |
| `SECURE_SSL_REDIRECT`, `SESSION_COOKIE_SECURE`, `CSRF_COOKIE_SECURE`, `SECURE_HSTS_SECONDS` | Réglages HTTPS/cookies/HSTS; à adapter au reverse proxy de production. |
| `DJANGO_MEDIA_ROOT` | Emplacement des documents téléversés; par défaut `media/`. |

Les origines CORS locales des fronts `localhost:3000` et `localhost:3001` sont
déclarées dans les paramètres. En mode debug, CORS est ouvert par le code :
ne déployez jamais une configuration `DEBUG` en production.

### Base de données

En développement, `DB_BACKEND` peut être `sqlite` ou `postgresql`.
`POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_HOST` et
`POSTGRES_PORT` décrivent la connexion PostgreSQL. Si PostgreSQL n’est pas
joignable en mode debug, `DB_ALLOW_SQLITE_FALLBACK` permet le repli SQLite;
vérifiez toujours les journaux pour connaître le moteur réellement sélectionné.

En production (`DJANGO_DEBUG=0`), `DATABASE_URL` est obligatoire et doit désigner
PostgreSQL. La configuration active le SSL pour cette URL. Ne passez pas une
URL réelle contenant un mot de passe dans un terminal partagé ou un journal.

**Ne partagez pas un même fichier SQLite entre plusieurs conteneurs ou processus
sur un montage Windows.** Le Compose du backend utilise un volume Docker
persistant pour les données SQLite. PostgreSQL est recommandé pour une
exploitation multi-services et la production.

### E-mail Django

Le backend propose un backend console par défaut; l’exemple est configuré pour
le fournisseur Brevo. Selon votre choix, renseignez :

- `EMAIL_PROVIDER` et `EMAIL_BACKEND`;
- les paramètres SMTP `EMAIL_HOST`, `EMAIL_PORT`, `EMAIL_USE_TLS`,
  `EMAIL_USE_SSL`, `EMAIL_TIMEOUT`, `EMAIL_HOST_USER`,
  `EMAIL_HOST_PASSWORD`;
- ou `BREVO_API_KEY`, `BREVO_SENDER_EMAIL` et `BREVO_SENDER_NAME`;
- `DEFAULT_FROM_EMAIL`.

L’e-mail du backend Django (activation du compte, par exemple) est une
configuration distincte des credentials SMTP gérés dans n8n. Un succès des
notifications n8n ne garantit donc pas que l’envoi Django soit configuré, et
réciproquement.

### Services internes, IA et paiement

- `DJANGO_INTERNAL_TOKEN` protège les échanges avec la passerelle et les routes
  internes prévues pour les services EcoScan.
- `AI_SERVICE_URL`, `AI_SERVICE_INTERNAL_TOKEN` et `AI_SERVICE_TIMEOUT`
  configurent l’accès au service d’IA. Dans le Compose principal, l’URL est
  remplacée par `http://ai-service:8001`.
- `PAYDUNYA_MASTER_KEY`, `PAYDUNYA_PRIVATE_KEY`, `PAYDUNYA_PUBLIC_KEY` et
  `PAYDUNYA_TOKEN` sont les identifiants PayDunya; `PAYDUNYA_MODE=test` permet
  de rester en mode de test.
- `BACKEND_BASE_URL` sert à construire les liens de callback. `PAYDUNYA_CALLBACK_URL`
  peut remplacer l’URL de callback construite par défaut.

Gardez chaque secret distinct de ceux des autres services et stockez-les dans
un gestionnaire de secrets adapté en production.

## Démarrage et migrations

Après chaque mise à jour du code ou ajout de migrations :

```powershell
python manage.py check
python manage.py migrate
```

En Docker, le `Dockerfile` applique actuellement les migrations (`migrate
--noinput`) au démarrage du conteneur backend, puis lance le serveur Django.
Les migrations doivent tout de même être revues avant un déploiement, et les
données doivent être sauvegardées avant une migration de production.

Créer une migration après une modification de modèle :

```powershell
python manage.py makemigrations
python manage.py makemigrations --check --dry-run
```

Les fichiers de migrations sont versionnés avec le code. Ne supprimez pas une
migration déjà appliquée et ne modifiez pas les données de production au moyen
d’une commande de diagnostic.

## Lancement avec Docker Compose

Le fichier `docker-compose.yml` principal définit :

- `backend` : API Django sur le port 8000;
- `ai-service` : service IA sur le réseau privé Compose;
- `energy-simulator` : simulateur limité aux organisations de démonstration
  configurées dans le Compose;
- `energy-jobs` : processus quotidien d’analyses énergétiques.

Le Compose utilise le `.env` local du backend et construit les images. Depuis
PowerShell :

```powershell
Set-Location 'C:\chemin\vers\ECOSCAN BACK-END\ecoscan-backend-v2'
Copy-Item .env.example .env
# Configurez .env et le dépôt/service IA avant le premier démarrage.
docker network create ecoscan-automation
docker compose config --quiet
docker compose up -d --build
docker compose ps
```

Le réseau externe `ecoscan-automation` doit être créé une seule fois avant le
premier démarrage (`docker network create ecoscan-automation`). Il relie le
backend à n8n pour les webhooks; n8n reste publié sur `127.0.0.1:5678` sur la
machine hôte. Le backend utilise le nom Docker `ecoscan-n8n` pour joindre n8n.

Le serveur est publié sur le port 8000. Consultez les journaux sans afficher
les variables d’environnement :

```powershell
docker compose logs --tail 100 backend
docker compose logs --tail 100 energy-simulator
docker compose logs --tail 100 energy-jobs
```

### Déploiement du jeu de démonstration public

Le dépôt peut contenir une copie SQLite de démonstration assainie. Elle conserve
les données synthétiques de sites, équipements et télémétrie, remplace les
hashes de mots de passe par des valeurs inutilisables, anonymise les autres
comptes et retire les sessions, journaux d’audit et traces d’administration.
Ne l’utilisez pas comme base de production client.

Pour créer une copie depuis une base locale sans modifier la source :

```powershell
python scripts/export_public_demo_database.py `
  --source db.sqlite3 `
  --output demo-data.sqlite3
```

Le script refuse d’écraser un fichier de destination existant. Vérifiez les
comptes et les données avant de publier l’export; ne publiez jamais le fichier
`db.sqlite3` d’origine ni une sauvegarde SQLite brute.

Pour démarrer une instance isolée dédiée à la démonstration, configurez d’abord
les variables normales de `.env`, puis préparez le fichier privé des mots de
passe à partir du modèle :

```powershell
Copy-Item demo-secrets.env.example demo-secrets.env
# Renseignez les quatre valeurs depuis le gestionnaire de secrets; ne les committez pas.
docker compose -f docker-compose.yml -f docker-compose.demo.yml config --quiet
docker compose -f docker-compose.yml -f docker-compose.demo.yml up -d --build
```

Cet environnement utilise le volume distinct `ecoscan-demo-sqlite-data` et y
copie la base assainie uniquement si le volume ne contient pas encore de base.
Il conserve les données d’un volume déjà initialisé. Le simulateur ne produit
que des relevés simulés et ne commande aucun matériel. La base de démonstration
comprend les organisations liées aux comptes pilote, multisite et anomalies.

Le compte lecture `demo.lecture@demo.ecoscan.test` est créé et rattaché à ces
trois organisations lors du provisionnement. Fournissez quatre mots de passe
distincts d’au moins 20 caractères par les variables de `demo-secrets.env` :
`ECOSCAN_DEMO_CONTROL_PASSWORD`, `ECOSCAN_DEMO_MULTISITE_PASSWORD`,
`ECOSCAN_DEMO_ANOMALIES_PASSWORD` et `ECOSCAN_DEMO_LECTURE_PASSWORD`. Ne mettez
pas ces valeurs dans Git, une image ou un journal. Après le démarrage, lancez :

```powershell
docker compose -f docker-compose.yml -f docker-compose.demo.yml `
  exec backend python manage.py provisionner_comptes_demo
```

La commande définit les quatre mots de passe à partir du fichier privé et ne
les affiche pas. Elle refuse les variables absentes, trop courtes ou
dupliquées, ainsi que les bases où les trois comptes pilote ne sont pas chacun
rattachés à une organisation distincte. Les mots de passe déjà partagés dans
une conversation doivent être changés et ne doivent pas être réutilisés.

Important : `demo.lecture` utilise le rôle `CONSULTANT`, mais l’API actuelle
n’impose pas une règle lecture seule uniforme à tous les endpoints de
monitoring. Ne traitez pas ce compte comme un rôle de sécurité en lecture seule;
il sert à la présentation du jeu de données et ne doit pas recevoir de données
ou d’accès sensibles.

Pour arrêter le projet sans supprimer les volumes de données :

```powershell
docker compose stop
```

`docker compose down` supprime les conteneurs et le réseau du projet, mais
conserve normalement les volumes nommés. N’ajoutez pas `--volumes` sans avoir
une sauvegarde et compris les données qui seront supprimées.

Le fichier `docker-compose.energy-jobs.yml` permet d’exécuter ponctuellement
les jobs quotidiens, notamment par le Planificateur de tâches Windows. Le
script `scripts/install-daily-energy-task.ps1` enregistre une tâche quotidienne
Windows qui appelle `scripts/run-daily-energy-jobs.ps1`; l’installation et
l’exécution de cette tâche sont des actions explicites de l’administrateur.

## Applications et API

Les routes détaillées sont distribuées dans les fichiers `urls.py` de chaque
application. Commencez par `config/urls.py`, puis consultez le routeur de
l’application concernée. Les réponses sont paginées par défaut (20 résultats
par page) et acceptent le paramètre d’ordre `ordering` lorsque l’endpoint le
permet.

### Authentification et autorisations

L’API REST utilise l’authentification JWT Simple JWT. Les fonctionnalités
protégées doivent être appelées avec le jeton d’accès retourné par le parcours
d’authentification. Les règles métier tiennent compte du rôle de l’utilisateur
et de son appartenance à une organisation. Les jetons internes de service ne
remplacent pas le JWT utilisateur et ne doivent pas être réutilisés entre
services.

Les routes `/api/internal/` sont réservées aux communications backend,
passerelle ou service IA. Ne les publiez pas directement sur Internet.

### Domaines métier

- **Organisations** : création et gestion de la structure, des utilisateurs
  membres, des sites et des compteurs.
- **Énergie** : import et consultation des relevés, factures, objectifs et
  prévisions de consommation.
- **Energy assets** : équipements, zones, capteurs réels ou simulés, mesures,
  état, commandes demandées et accusés de réception.
- **Analyses** : contexte énergétique, diagnostics, recommandations, rapports,
  alertes et opportunités de financement.
- **Audit** : événements métier et suivi des notifications.
- **Billing** : catalogue de plans, essais, abonnements, factures et paiements.

Un `input_boolean` Home Assistant ou un capteur en mode `SIMULATED` ne doit
jamais être présenté dans les rapports comme une télémétrie issue d’un compteur
physique.

## Capteurs, simulation et monitoring

`simuler_capteurs` génère des relevés uniquement pour les capteurs actifs de
type `SIMULATED`; les capteurs réels sont laissés intacts. La simulation
respecte la fréquence configurée par capteur et synchronise les mesures avec
l’état des équipements et l’énergie cumulée.

Exécuter le simulateur seul :

```powershell
python manage.py simuler_capteurs
```

Options utiles :

```powershell
python manage.py simuler_capteurs --once
python manage.py simuler_capteurs --interval 2
python manage.py simuler_capteurs --organisation-id <UUID>
```

`--interval` règle la fréquence de vérification du processus, pas la fréquence
de mesure propre au capteur. Répétez `--organisation-id` pour plusieurs
organisations. Sans filtre, tous les capteurs simulés actifs admissibles peuvent
être traités.

Le projet fournit aussi des diagnostics d’occupation par zone. Une zone est
`UNOCCUPIED` seulement si ses capteurs de présence actifs confirment une
absence récente; une donnée absente ou périmée donne un état inconnu et ne
prouve jamais l’inoccupation. Les analyses et décisions autonomes restent
limitées aux capteurs simulés et aux scénarios explicitement autorisés.

La commande `creer_donnees_test_ecoscan` installe ou actualise un jeu de données
de pilote pour l’organisation réservée à EcoScan. Elle définit le même mot de
passe temporaire pour ses comptes de démonstration :

```powershell
python manage.py creer_donnees_test_ecoscan --password "<mot-de-passe-temporaire-long>"
```

Cette commande écrit des données dans la base. Ne l’exécutez pas sur une base
réelle. Le mot de passe doit respecter la longueur minimale vérifiée par la
commande et ne doit pas être réutilisé.

Voir [le guide Energy Assets](energy_assets/README.md) pour les comportements
précis du simulateur, l’occupation, les anomalies, les actions virtuelles et
les mesures d’impact.

## Analyses et traitements planifiés

La commande `executer_taches_energetiques_quotidiennes` regroupe les contrôles
quotidiens configurés : détection d’anomalies de télémétrie, alertes
énergétiques, comparaison de factures et progression des objectifs, selon les
services actifs. La commande `executer_taches_energetiques_planifiees` attend
l’heure locale configurée dans `ECOSCAN_TACHES_HEURE` (défaut `02:15`) puis
lance cette routine chaque jour.

Le Compose principal démarre le service `energy-jobs`. Une autre option est le
Planificateur de tâches Windows :

```powershell
.\scripts\install-daily-energy-task.ps1 -StartTime '02:15'
```

Le script nécessite Docker, un `.env` backend configuré et un utilisateur
Windows autorisé à créer la tâche. La tâche s’exécute lorsque la session
Windows est ouverte et que Docker Desktop est disponible. Les journaux sont
écrits sous `%LOCALAPPDATA%\EcoScan\logs`.

Les commandes d’analyse ciblée existent également, notamment :

```powershell
python manage.py analyser_anomalies_telemetrie
python manage.py analyser_occupation
python manage.py mesurer_impacts_telemetrie
```

Ces commandes peuvent créer ou actualiser des anomalies, recommandations,
mesures d’impact et événements d’audit. Testez-les sur une base de
développement; ne les lancez pas uniquement pour vérifier la connectivité.

Les tâches de facturation sont distinctes des jobs énergétiques. La commande
`executer_taches_billing` peut expirer des essais, traiter des renouvellements
ou déclencher des relances. Elle peut modifier des statuts et provoquer des
notifications externes :

```powershell
python manage.py executer_taches_billing --expiration
python manage.py executer_taches_billing --renouvellements
python manage.py executer_taches_billing --relances
```

Sans option, elle exécute toutes les catégories. Ne planifiez que les options
nécessaires et ne testez pas cette commande sur des abonnements réels sans
validation métier.

## Facturation et paiements

Le module `billing` gère les plans, essais, périodes de grâce, renouvellements,
factures et relances. Le catalogue initial peut être préparé avec la commande
`initialiser_catalogue_billing`; vérifiez son comportement dans le code avant
de l’exécuter sur une base partagée.

PayDunya est configuré par `PAYDUNYA_MODE` et les quatre variables de clés
PayDunya. Restez en mode test jusqu’à avoir configuré les clés de test et
confirmé les URLs de callback. En production, utilisez une URL HTTPS publique
pour le webhook PayDunya (`PAYDUNYA_CALLBACK_URL`) et vérifiez les signatures
et retours du fournisseur.

Les relances envoyées par n8n prennent en charge le canal e-mail. Les canaux
SMS, WhatsApp et notification interne ne sont pas fournis par ce workflow.
Pour le détail du callback et des statuts, consultez `billing/urls.py`,
`billing/views.py` et le guide n8n.

## Automatisations n8n

Le répertoire `n8n/workflows/` contient des exports JSON importables. Leur
présence sur disque ne signifie pas qu’ils sont importés ou activés dans une
instance n8n. Après un import, associez les credentials nécessaires, validez
les URLs et secrets, testez dans un environnement non productif, puis activez
chaque workflow séparément.

| Export | Déclenchement | Dépendances principales |
| --- | --- | --- |
| `ecoscan-anomaly-notification.json` | Webhook Django `/webhook/anomalie-detectee`. | `N8N_WEBHOOK_BASE_URL`, jeton partagé `N8N_WEBHOOK_TOKEN`, credential SMTP. |
| `ecoscan-energy-alert-notification.json` | Webhook Django `/webhook/alerte-energetique`. | Même base et jeton webhook, SMTP et job énergétique qui émet l’alerte. |
| `ecoscan-billing-reminder.json` | Webhook Django `/webhook/ecoscan-relance`. | URL `N8N_RELANCE_WEBHOOK_URL`, secret distinct `N8N_SHARED_SECRET`, URL backend joignable et SMTP. La tâche de facturation doit aussi être planifiée. |
| `ecoscan-nightly-climate-audit.json` | Horaire quotidien de 22 h 45 (fuseau configuré dans n8n). | Accès en lecture à Home Assistant, liste explicite d’entités autorisées, destinataire et SMTP. |
| `ecoscan-opportunities-ingestion.json` | Manuel et collecte quotidienne. | Clé Groq, endpoint d’ingestion et `N8N_INGESTION_TOKEN` identique côté Django. Peut créer ou modifier des opportunités. |

Les URL internes de la configuration Docker de ce projet utilisent le nom du
conteneur/service sur le réseau partagé. En développement natif, remplacez-les
par des URL accessibles depuis le processus qui émet la requête. `localhost`
dans un conteneur désigne ce conteneur, pas Windows ni un autre conteneur.

Les secrets ont des usages distincts :

- `N8N_WEBHOOK_TOKEN` authentifie les événements Django vers les webhooks
  d’anomalie et d’alerte énergétique.
- `N8N_SHARED_SECRET` protège le flux de relance et son callback. Sa valeur
  doit être distincte du jeton webhook.
- `N8N_INGESTION_TOKEN` protège l’ingestion d’opportunités; conservez la valeur
  déjà configurée côté Django si elle existe.

Le workflow d’audit accepte des entités `climate.*` ou des entités de
démonstration `input_boolean.ecoscan_demo_climatisation_*`, mais pas les deux
familles à la fois. Les `input_boolean` sont explicitement rapportés comme
simulation. Ce workflow lit les états et envoie un rapport; il n’appelle aucun
service de commande Home Assistant.

**Limites connues :** les alertes d’approche de tranche ne sont pas émises tant
que les règles métier de période et de remise à zéro ne sont pas confirmées.
Le client Django contient un helper d’événement d’onboarding, mais le parcours
actuel ne l’appelle pas. La collecte d’opportunités et les relances peuvent
modifier des données ou envoyer des e-mails; effectuez les tests en staging.

Configuration complète, variables, étapes d’import et procédures de test :
[n8n/workflows/README.md](n8n/workflows/README.md). Un environnement local
n8n possède son propre fichier de variables; ne copiez jamais ses valeurs
secrètes dans un export de workflow ou dans Git.

### Premier démarrage de n8n avec le Compose fourni

Le Compose n8n utilise un volume externe persistant `n8n_data` et le réseau
externe partagé `ecoscan-automation`. Créez chacun une seule fois si nécessaire
et préparez une configuration locale sans écraser un fichier existant :

```powershell
Set-Location 'C:\chemin\vers\ECOSCAN BACK-END\ecoscan-backend-v2'
# À exécuter une seule fois; ignorez chaque commande si la ressource existe déjà.
docker network create ecoscan-automation
docker volume create n8n_data

Set-Location .\n8n
if (-not (Test-Path .env.local)) {
    Copy-Item .env.example .env.local
}
# Modifiez .env.local dans un éditeur local; ne le versionnez pas.
docker compose --env-file .env.local config --quiet
docker compose --env-file .env.local up -d
```

Renseignez les variables obligatoires signalées par la validation Compose.
Pour les appels backend depuis les conteneurs, les valeurs par défaut du réseau
partagé sont `BACKEND_BASE_URL=http://backend:8000`,
`ECOSCAN_INGESTION_URL=http://backend:8000/...` et côté Django
`N8N_WEBHOOK_BASE_URL=http://ecoscan-n8n:5678/webhook`. Adaptez les URL si la
topologie change. Après une modification du fichier d’environnement, recréez
le service avec la même option `--env-file .env.local`.

Avant d’activer un workflow, importez son export JSON dans n8n, associez les
credentials, puis vérifiez les URLs et secrets. L’export n’est pas activé
automatiquement. Testez dans un environnement contrôlé; un webhook de
production peut envoyer un e-mail ou modifier des données.

## Passerelle Home Assistant

La passerelle `gateway/home_assistant_agent.py` interroge Django pour prendre
une commande, mappe l’UUID d’un équipement vers une entité Home Assistant,
envoie la commande, relit l’état et renvoie un accusé de réception à Django.
Les accusés de réception en attente peuvent être persistés dans une outbox
locale.

Configuration :

1. Copiez `gateway/.env.example` vers `gateway/.env`.
2. Configurez l’URL Django, l’URL Home Assistant et le même
   `DJANGO_INTERNAL_TOKEN` que le backend.
3. Copiez `gateway/equipment-entities.example.json` vers
   `gateway/equipment-entities.json`.
4. Remplacez les exemples par les associations UUID EcoScan → entités
   actionneurs approuvées.
5. Vérifiez la configuration avant de démarrer le service.

Pour lancer l’instance Home Assistant de développement, configurez d’abord
`gateway/.env` et `gateway/equipment-entities.json`, puis composez les fichiers
Compose afin que backend, Home Assistant et passerelle partagent le réseau :

```powershell
docker compose `
  --file .\docker-compose.yml `
  --file .\docker-compose.home-assistant.yml `
  --file .\docker-compose.gateway.yml `
  config --quiet

docker compose `
  --file .\docker-compose.yml `
  --file .\docker-compose.home-assistant.yml `
  --file .\docker-compose.gateway.yml `
  up --build -d
```

L’instance de développement Home Assistant est distincte d’une installation
existante. Terminez son assistant initial dans le navigateur et créez un jeton
d’accès longue durée réservé à la passerelle. Les `input_boolean` des exemples
permettent de tester un état ON/OFF virtuel; ils ne pilotent aucun appareil.

La passerelle peut commander les entités explicitement mappées. Commencez par
un actionneur de test non critique, confirmez le retour d’état, prévoyez un
arrêt manuel indépendant et faites réaliser toute installation électrique par
un professionnel qualifié. L’état Home Assistant est le retour logiciel du
contrôleur, pas une mesure de courant. Ne publiez pas `/api/internal/` sur
Internet.

Détails, lancement natif, outbox et tests de la passerelle :
[gateway/README.md](gateway/README.md).

## Tests et contrôles

Depuis le répertoire `ecoscan-backend-v2`, avec les dépendances et un
environnement de test configurés :

```powershell
python manage.py check
python manage.py makemigrations --check --dry-run
python manage.py test
python -m unittest gateway.test_home_assistant_agent
```

`manage.py test` utilise la base de test Django. Malgré cela, exécutez les tests
sur un environnement dédié et lisez les tests concernés avant d’ajouter une
suite d’intégration qui contacte des services externes.

Le conteneur backend fournit une vérification de santé via la page de connexion
de l’admin. Le service n8n a son propre `/healthz`. Un statut HTTP de santé ne
prouve ni qu’un webhook est configuré, ni qu’un workflow est actif, ni que
SMTP, PayDunya, l’IA ou Home Assistant sont authentifiés.

## Fichiers, données et sauvegardes

- `media/` contient les documents importés et générés.
- `fichiers-sources/` et `livrables/` sont des répertoires de travail utilisés
  par le service.
- SQLite local est créé dans `db.sqlite3` (ou dans `SQLITE_DB_PATH`).
- Docker conserve la base du backend dans le volume
  `ecoscan-backend-v2_ecoscan-sqlite-data` selon le nom de projet Compose.
- Home Assistant conserve sa configuration locale dans
  `home-assistant/config/`.
- n8n persiste ses workflows et credentials chiffrés dans son volume externe
  `n8n_data`.
- La passerelle persiste les callbacks en attente dans son volume `gateway_outbox`.

Sauvegardez base de données, documents et configuration Home Assistant
conjointement avant une mise à jour importante. Ne versionnez ni bases SQLite,
ni sauvegardes, journaux, médias privés, fichiers `.env`, mapping réel des
équipements ou tokens.

## Sécurité et mise en production

La configuration fournie est principalement destinée au développement local.
Avant toute mise en production :

1. Définissez `DJANGO_DEBUG=0`, une `DJANGO_SECRET_KEY` aléatoire, une
   `DATABASE_URL` PostgreSQL et des hôtes autorisés stricts.
2. Terminez TLS au reverse proxy et configurez les cookies sécurisés, HTTPS,
   HSTS et les en-têtes du proxy selon l’infrastructure.
3. Restreignez CORS aux origines nécessaires et vérifiez les permissions de
   chaque endpoint.
4. Stockez les secrets dans le gestionnaire de secrets du déploiement, jamais
   dans Git, les exports n8n, les journaux ou les tickets.
5. Séparez les jetons n8n, Django interne, IA, Home Assistant et PayDunya;
   donnez à chaque intégration les privilèges minimums.
6. Limitez l’accès réseau aux routes internes et à l’interface n8n. Le Compose
   n8n local lie son interface à `127.0.0.1`.
7. Utilisez des credentials SMTP dédiés; vérifiez le domaine expéditeur pour un
   service d’envoi réel. Mailtrap Email Testing est destiné aux tests et
   capture les messages au lieu de les livrer.
8. Sauvegardez la base et les fichiers, appliquez les migrations de manière
   contrôlée, puis vérifiez les health checks et journaux.
9. Gardez les automatismes de commande Home Assistant désactivés tant que le
   mapping, les règles de sécurité et l’arrêt d’urgence ne sont pas validés sur
   un pilote matériel distinct.

N’utilisez pas un `.env.example` tel quel en production. Les valeurs
`replace-with-*`, `change-me-*`, comptes `example.test` et URLs locales sont
des exemples à remplacer ou à ne pas utiliser.

## Dépannage

### Django ne démarre pas

- Vérifiez la présence du `.env` sans afficher son contenu dans les logs.
- Lancez `python manage.py check` et inspectez le journal du service backend.
- En production, confirmez que `DJANGO_DEBUG=0` et `DATABASE_URL` sont
  renseignés.
- Vérifiez PostgreSQL et les migrations.

### SQLite semble utiliser une mauvaise base

Contrôlez `SQLITE_DB_PATH`, le moteur indiqué dans les journaux au démarrage et
le volume effectivement monté. En mode debug, SQLite peut être sélectionné
volontairement ou utilisé comme repli si PostgreSQL est indisponible.

### OCR PDF ou image incomplet

Vérifiez Tesseract, les données `fra`, Poppler, la qualité du scan et le sens
des pages. Le score OCR mesure la qualité de lecture et ne constitue pas une
garantie de justesse de chaque champ extrait; les factures doivent être
contrôlées et validées.

### Une notification n’arrive pas

Vérifiez que le workflow n8n est importé et activé, que le backend utilise
l’URL de production `/webhook/...` (et non `/webhook-test/...`), que le token
attendu correspond côté Django et n8n, et que les conteneurs sont joignables
sur leur réseau. Ensuite, consultez l’exécution n8n et le credential SMTP.
L’envoi best-effort peut échouer sans annuler l’opération métier ayant créé
l’événement.

### Une relance n’est pas déclenchée

Vérifiez la planification de `executer_taches_billing`, le canal de relance
`EMAIL`, la présence d’adresses administrateur valides, `N8N_SHARED_SECRET`,
`N8N_RELANCE_WEBHOOK_URL`, `BACKEND_BASE_URL` et le callback Django. Les essais
de relance ont des effets métier; validez-les uniquement avec des données et
destinataires de test.

### La passerelle ne confirme pas une commande

Vérifiez le token interne, les URLs accessibles depuis la passerelle, le
mapping UUID → entité, les droits du jeton Home Assistant, l’état retourné et
les journaux d’outbox. L’accusé de réception indique l’état rapporté par Home
Assistant et ne mesure pas le courant électrique.

## Documentation complémentaire

- [Energy Assets : simulation, monitoring, occupation, anomalies et actions](energy_assets/README.md)
- [Passerelle Home Assistant](gateway/README.md)
- [Workflows n8n](n8n/workflows/README.md)
- [Variables Django sans secrets](.env.example)
- [Variables passerelle sans secrets](gateway/.env.example)
- [Variables n8n sans secrets](n8n/.env.example)
