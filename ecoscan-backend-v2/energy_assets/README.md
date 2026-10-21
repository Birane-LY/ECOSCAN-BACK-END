# Energy assets

## Jeu de données pilote local

Depuis la racine du backend, créez un jeu d'essai ciblé sans supprimer ni
réinitialiser les autres données :

```powershell
python manage.py creer_donnees_test_ecoscan --password "<mot de passe temporaire de 20 caractères minimum>"
```

La commande prépare l'organisation EcoScan de démonstration, ses deux sites,
trois comptes, neuf équipements, dix-huit capteurs simulés, des relevés
historiques, deux compteurs, des horaires, un objectif et une recommandation.
Elle ne modifie que les enregistrements portant les noms ou identifiants réservés
au pilote ; les relevés horodatés sont ajoutés à mesure que le temps avance.

Les comptes sont `pilot.ecoscan@example.test`,
`gestionnaire.ecoscan@example.test` et `consultant.ecoscan@example.test`.
Le mot de passe fourni à la commande est défini pour ces trois comptes. Gardez-le
temporaire et ne le réutilisez pas pour un compte réel. Les capteurs du jeu pilote
sont virtuels : le simulateur met à jour leur télémétrie, sans commander de
matériel physique.

## Capteur simulator

From the `ecoscan-backend-v2` directory, initialize or update the database
schema before the first run:

```powershell
python manage.py migrate
```

Use the same Python environment and database configuration for migration and
simulation. Run the simulator as a separate process alongside Django:

```powershell
python manage.py simuler_capteurs
```

The command checks active `SIMULATED` sensors once per second and emits a
measurement when each sensor's `frequence_secondes` interval has elapsed. Use
`python manage.py simuler_capteurs --once` to run one check, or
`--interval 2` to change the polling interval. The polling interval does not
change each sensor's configured measurement frequency. Repeat
`--organisation-id <UUID>` to explicitly target more than one organization;
without it, all active simulated sensors are eligible.

`POWER` sensors report a smoothly varying estimate around 92–108% of nominal
power while on, and the configured standby power while off. The simulator
follows the desired state first, then the reported state; if both are unknown,
it starts the simulated equipment as on. `ENERGY` sensors integrate the average
simulated power over the interval into a cumulative kWh reading. Unsupported
sensor types and real sensors are left untouched.

The generated measurements use the regular telemetry synchronization service,
so they update equipment state, cumulative energy, and sensor communication
timestamps in the same way as ingested readings. The simulator does not send
commands to physical equipment.

On Windows without Docker, run `.\scripts\start-local.ps1` from the backend
directory. It starts Django if needed and ensures one sensor simulator is
running against the same local database. Pass
`-SensorPollIntervalSeconds 5` to reduce its one-second default polling
interval. Backend and simulator logs are written under
`%LOCALAPPDATA%\EcoScan\logs`.

For the local mobile pilot with Docker, start the backend and its dedicated
simulator with `docker compose up -d --build`. The simulator is explicitly
limited to the pilot organization and the EcoScan demo bakery organization by
UUID. The bakery's five simulated `ENERGY` sensors use a 15-second measurement
interval so the daily estimate refreshes during a demo.
Simulated power is a varying estimate based on equipment nominal/standby power
and its requested state, not a physical meter reading. For a simulated energy counter
without a reading before midnight, the displayed daily total starts at its first
reading that day; time before that first reading is not reconstructed.

The backend, simulator, and scheduled jobs share the persistent Docker volume
`ecoscan-sqlite-data` for SQLite. Do not mount the same SQLite file from the
Windows workspace into multiple containers.

## Occupation des zones et diagnostic énergétique

Les capteurs de présence sont rattachés à une zone, distinctement des capteurs
de puissance attachés aux équipements :

- `POST /api/energy-assets/capteurs-presence/` crée un capteur avec une zone,
  un identifiant et un mode `SIMULATED` ou `REAL`. Pour un capteur simulé,
  `occupation_simulee` configure l'état émis à chaque cycle ; sans configuration,
  aucun état n'est émis et l'occupation reste inconnue. Ce champ ne peut pas être
  renseigné sur un capteur réel.
- `POST /api/energy-assets/mesures-presence/` enregistre `capteur`,
  `occupee` (booléen) et `date_mesure`. Les mesures sont consultables sur la
  même ressource en `GET`.
- `GET /api/energy-assets/sites/{site_id}/occupancy-diagnostics/` retourne
  l'état des zones actives et la puissance mesurée des équipements surveillés.

Une zone est considérée `UNOCCUPIED` uniquement si tous ses capteurs de
présence actifs ont transmis récemment un état inoccupé. Les relevés plus vieux
que trois intervalles configurés (avec un minimum d'une minute), un capteur
sans relevé, ou l'absence de capteurs produisent l'état `UNKNOWN` ; un signal
d'occupation récent donne priorité à `OCCUPIED`. Ainsi, l'absence de télémétrie
ne devient jamais une preuve d'inoccupation.

Le diagnostic estime la puissance évitable à partir de la puissance `POWER`
récente au-delà de la puissance de veille configurée. Cette valeur est une
puissance instantanée en kW, pas une économie d'énergie garantie en kWh. Le
diagnostic est en lecture seule : il ne crée ni anomalie, ni recommandation, ni
commande.

Le simulateur `simuler_capteurs` émet également les états des capteurs de
présence simulés actifs, sans modifier ni imiter les capteurs réels. Les
anomalies de consommation en zone inoccupée sont calculées par
`python manage.py analyser_occupation` et incluses dans
`GET /api/energy-assets/sites/{site_id}/anomalies/`. L'analyse intègre les
relevés POWER par trapèzes, conserve uniquement les intervalles où tous les
capteurs de présence actifs confirment une absence fraîche, et requiert au
moins une heure exploitable. Les données insuffisantes ou périmées ne ferment
pas une anomalie. La détection et la recommandation associée sont idempotentes ;
la recommandation reste une proposition à valider par un humain.

L'autopilotage simulé peut utiliser `occupation_active` et `zones_autorisees`
dans `PATCH /api/energy-assets/sites/{site_id}/autonomie/`. Il arrête seulement
des équipements autorisés, non critiques, avec télémétrie de puissance
simulée, dans une zone explicitement autorisée et confirmée inoccupée par des
capteurs de présence simulés récents. Une donnée réelle, absente ou périmée
bloque cette stratégie ; aucun ordre physique n'est envoyé.

## Consumption anomaly detection

Apply the V2 migrations before running the analysis:

```powershell
python manage.py migrate
```

Run `python manage.py analyser_anomalies_telemetrie` after daily energy readings
are available. By default, it analyzes the previous local calendar day for
each site. Use `--date YYYY-MM-DD` to select a local day and `--site <UUID>` to
analyze a single site.

For a site to be analyzed, each active `ENERGY` sensor on monitored equipment
must have at least four readings on the analyzed day and on each of the four
previous matching weekdays. Daily usage is the difference between the first
and last cumulative reading for that day. If any day or sensor lacks sufficient
readings, the analysis reports insufficient data and creates no anomaly.

The observed daily usage is compared with the simple average of the four
reference days, following the existing analysis baseline quality requirements.
Variations below 10% are considered normal; 10%, 20%, and 40% mark the
surveillance, alert, and priority-investigation thresholds used by the existing
anomaly service. Detected anomalies are available at
`/api/energy-assets/sites/{site_id}/anomalies/`.

## Actions virtuelles

Les actions virtuelles estiment l'effet énergétique d'un changement d'état d'un
équipement dans le contexte d'une recommandation. Elles ne modifient ni l'état
souhaité/rapporté ni la file de commandes Home Assistant.

### API

- `POST /api/energy-assets/actions-virtuelles/` crée une simulation.
- `GET /api/energy-assets/actions-virtuelles/` liste les simulations des
  organisations accessibles à l'utilisateur.
- Le paramètre `?recommandation=<UUID>` limite la liste à une recommandation.

Exemple de création :

```json
{
  "recommandation": "UUID_RECOMMANDATION",
  "equipement": "UUID_EQUIPEMENT",
  "etat_cible": "OFF",
  "duree_heures": "4.00"
}
```

La recommandation et l'équipement doivent appartenir à la même organisation,
l'utilisateur doit disposer d'un abonnement actif et l'équipement doit avoir
un état rapporté `ON` ou `OFF`. La durée doit être comprise entre 0,01 et 24
heures.

### Estimation

La puissance de référence utilise la dernière puissance positive de l'équipement
quand son état rapporté est `ON`. À défaut, elle utilise la puissance nominale
multipliée par la quantité. Pour un équipement rapporté `OFF`, elle utilise la
puissance de veille multipliée par la quantité. La puissance du scénario cible
utilise la puissance nominale (`ON`) ou la puissance de veille (`OFF`), également
multipliée par la quantité.

`variation_energie_kwh` est calculée par
`(puissance de référence - puissance du scénario) × durée`. Une valeur positive
est une économie potentielle ; une valeur négative est une hausse potentielle.
Les résultats sont des estimations fondées sur les puissances disponibles, pas
une mesure garantie de consommation ni une promesse d'économie.

## Mesure des économies réelles des actions

Associez l'action terminée à l'équipement suivi pour mesurer son impact. La
mesure utilise un unique capteur `ENERGY` réel et actif ; les capteurs simulés
ne sont jamais considérés comme une preuve d'économie réelle.

La journée de réalisation est exclue. Pour une réalisation le jour D, le backend
compare les sept jours D-7 à D-1 avec les sept jours D+1 à D+7. Chaque journée
des deux périodes doit contenir au moins quatre relevés cumulatifs cohérents.
Un index décroissant, un capteur
absent ou plusieurs capteurs admissibles empêche la mesure ; aucun résultat
partiel n'est présenté comme une économie confirmée. Une hausse de consommation
est conservée comme une économie négative.

Les valeurs `energie_reference_kwh`, `energie_post_action_kwh`,
`economie_energie_kwh` et `capteur_impact` sont exposées sur l'API des actions.
La progression mesurée d'un objectif en kWh (ou en %) calcule à la consultation
la somme des économies télémétriques quand toutes ses actions terminées dans la
période sont mesurables. Cette consultation reste en lecture seule. La mesure
reste distincte de la progression déclarée et des économies estimées ; aucun
tarif n'est supposé.

## Planification quotidienne sous Windows et Docker

### Backend V2 permanent dans Docker Desktop

Depuis la racine `ecoscan-backend-v2`, configurez `.env` à partir de
`.env.example` si nécessaire et vérifiez la connexion configurée à la base.
Démarrez le backend avec Docker Desktop :

```powershell
docker compose config --quiet
docker compose up --build -d
docker compose ps
```

Le conteneur s'appelle `ecoscan-backend-v2` et expose l'API sur
`http://localhost:8000`. Son démarrage applique les migrations avant Django.
La base SQLite persistante se trouve dans le volume Docker
`ecoscan-sqlite-data`, partagé avec le simulateur et le conteneur des tâches
quotidiennes. Les dossiers de fichiers restent partagés avec l'hôte. Depuis
n8n ou la passerelle dans un autre réseau Docker, utilisez
`http://host.docker.internal:8000` plutôt que `localhost`.

Consultez les journaux avec `docker compose logs --follow backend`, arrêtez le
backend avec `docker compose down` et redémarrez-le avec
`docker compose restart backend`. Ne supprimez pas les volumes ou fichiers de
données pour arrêter le service.

Le point d'entrée `executer_taches_energetiques_quotidiennes` exécute dans
l'ordre l'analyse quotidienne des compteurs, factures et relevés rituels, la
détection des anomalies de télémétrie des sites, la mesure des impacts des
actions terminées, puis l'analyse des zones inoccupées. Les diagnostics et
recommandations des anomalies de télémétrie des sites sont planifiés en
arrière-plan; l'analyse des compteurs et du rituel les génère dans son
traitement quotidien. Les mesures d'impact qui n'ont pas encore sept jours
complets après réalisation seront réessayées lors des exécutions suivantes.

Le service `energy-jobs` du compose principal planifie cette chaîne chaque jour
à 02:15 dans le fuseau Django (`TIME_ZONE`). Il attend que l'API soit saine et
réutilise le même volume de base. Pour changer l'heure, définissez
`ECOSCAN_TACHES_HEURE=HH:MM` dans l'environnement avant `docker compose up`.
L'analyse n'est pas exécutée immédiatement au démarrage : elle attend le
prochain créneau. Sur Windows, le Planificateur de tâches fourni plus bas est
une alternative ponctuelle; ne l'activez pas en même temps que le service
compose afin d'éviter des exécutions quotidiennes en double.

Configurez d'abord `.env` à partir de `.env.example` et vérifiez que la base
configurée est persistante et joignable depuis Docker. Le fichier
`docker-compose.energy-jobs.yml` réutilise cette configuration et le volume
SQLite `ecoscan-sqlite-data` du backend. Pour PostgreSQL exécuté directement sur
Windows, il rend l'hôte accessible au conteneur via `host.docker.internal`.
Pour une base PostgreSQL distante, configurez sa connexion dans `.env`.

Depuis PowerShell, avec Docker Desktop démarré et depuis ce dossier backend :

```powershell
Copy-Item .env.example .env  # À faire uniquement si .env n'existe pas déjà.
# Renseigner les valeurs adaptées à l'environnement dans .env.
docker compose --file docker-compose.energy-jobs.yml config --quiet
docker compose --file docker-compose.energy-jobs.yml run --rm --no-deps energy-jobs python manage.py check
```

Appliquez les migrations lors du déploiement backend, avant d'activer la tâche.
Pour effectuer un essai manuel des traitements, lancez
`.\scripts\run-daily-energy-jobs.ps1`. La commande exécute l'image backend dans
un conteneur ponctuel et conserve ses journaux sous
`%LOCALAPPDATA%\EcoScan\logs\daily-energy-jobs.log`.

Pour planifier chaque jour à 02:15 (heure locale) avec le Planificateur de
tâches Windows :

```powershell
.\scripts\install-daily-energy-task.ps1
```

Une autre heure peut être indiquée, par exemple
`.\scripts\install-daily-energy-task.ps1 -StartTime "03:00"`. La tâche est
enregistrée pour l'utilisateur Windows courant et s'exécute uniquement lorsque
sa session est ouverte et que Docker Desktop est disponible. Activez la tâche
après un essai manuel concluant et vérifiez son historique ainsi que son journal.
Gardez les commandes unitaires disponibles pour une exécution ciblée :

```powershell
python manage.py analyser_anomalies_telemetrie
python manage.py mesurer_impacts_telemetrie
```

## Administrator notifications

When an anomaly is first detected, or is detected again after being resolved,
Django posts an `anomalie-detectee` event to
`{N8N_WEBHOOK_BASE_URL}/anomalie-detectee` using the existing
`X-N8N-Webhook-Token` authentication. Set `N8N_WEBHOOK_BASE_URL` to the n8n
production webhook prefix, normally `https://<n8n-host>/webhook`. The payload
includes the anomaly values, site and organization details, active organization
administrator email addresses, and the site anomaly API URL. Re-running an
analysis for an anomaly that is already active does not send a duplicate event.

Configure an authenticated n8n webhook workflow for the `anomalie-detectee`
path and use its organization administrator email list to deliver the
notification. Configure the matching `X-N8N-Webhook-Token` credential and any
email or messaging credentials in n8n; no delivery credentials belong in Django
or in a workflow export. Event delivery uses the existing best effort n8n
client, so connection failures are logged and do not roll back anomaly
detection.

## Autonomie MAXI en simulation

L'écran **Plus > Autonomie MAXI** configure une politique distincte par site.
Elle est désactivée par défaut et ne pilote jamais Home Assistant : les
transitions sont appliquées uniquement à l'état simulé EcoScan des équipements
explicitement autorisés. Les équipements de criticité haute ou critique ne
peuvent pas être autorisés.

Les stratégies configurables sont un plafond de puissance (kW), une plage
horaire, une plage de confort climatique alimentée par une température de
scénario saisie dans l'app, et un arbitrage automatique. Cet arbitrage traite,
dans l'ordre, le dépassement du plafond, le confort puis les horaires. Le
plafond, les horaires et la température de scénario sont des estimations ou des
hypothèses : ils ne constituent pas une mesure physique. La stratégie
climatique requiert un équipement de catégorie `CLIMATISATION` autorisé.

L'API de configuration est
`/api/energy-assets/sites/{site_id}/autonomie/` (`GET` et `PATCH`) ; un `POST`
sur cette route lance une évaluation immédiate. L'arrêt d'urgence est activé
immédiatement en envoyant `{"arret_urgence": true}` par `PATCH`; il désactive
aussi le pilote. Le journal conserve la règle choisie, sa raison, l'état et la
puissance simulés avant et après. Une évaluation applique au maximum un
changement d'état et le simulateur continu l'évalue ensuite au plus une fois
par minute. Pour exécuter ce cycle, le service `energy-simulator` doit être
actif. `python manage.py simuler_capteurs --once` génère un cycle de mesures et
évalue immédiatement les politiques actives pour un essai ciblé.

Un journal marqué `APPLIED` signifie **appliqué en simulation**, jamais qu'un
appareil physique a obéi. Les commandes autonomes ne sont pas ajoutées à la
file Home Assistant.

## Contrôle des documents énergétiques

Les fichiers déposés via `/api/energies/fichiers-sources/` sont limités à
25 Mo et aux formats PDF, JPG, PNG, WebP, TXT et CSV. Les signatures des
formats binaires sont vérifiées; renommer un fichier ne suffit pas à le faire
accepter. Le parcours `/api/energies/capture-facture/` n'accepte que JPG, PNG
ou WebP et conserve les champs de facture explicitement autorisés.

L'OCR et les mots-clés ne prouvent ni l'authenticité de la facture ni son
origine sénégalaise. Un document hors périmètre est rejeté; tout candidat
énergétique reste en `REVUE_REQUISE`, sans publication de métrique. Seul un
administrateur de l'organisation peut appeler
`POST /api/energies/imports/{import_id}/valider-et-publier/`, en transmettant
`{"origine_senegalaise_confirmee": true, "authenticite_confirmee": true,
"emetteur_confirme": "SENELEC"}` après examen visuel du
document. La revue et son auteur sont conservés dans le rapport d'analyse;
`verification_officielle` reste faux,
car aucune source officielle n'est interrogée. L'intégration directe d'un
import en revue est refusée. Une attestation humaine ne doit donc pas être
présentée comme une vérification cryptographique ou officielle.
Les tickets de recharge Woyofal sont exclus du flux de factures : le montant
payé et les kWh crédités sont des données distinctes à saisir depuis le ticket,
et aucun des deux ne doit être transformé en consommation.

## Alertes quotidiennes et comparaison de périodes

La routine `executer_taches_energetiques_quotidiennes` évalue aussi les alertes
de solde/autonomie Woyofal, d'approche de tranche, de comparaison facture-
monitoring et de progression mesurée des objectifs actifs. Les alertes sont
envoyées aux administrateurs actifs par le webhook n8n
`/alerte-energetique`; elles sont dédupliquées via le journal d'audit et les
tentatives échouées peuvent être rejouées.

Les alertes de solde ne s'appuient que sur un relevé déclaré récent (trois
jours au plus). Les barèmes d'énergie CRSE applicables depuis le 1er janvier
2026 ([grille officielle](https://www.crse.sn/wp-content/uploads/2025/12/Grille-tarifaire-Senelec-a-compter-du-01012026.pdf))
distinguent les catégories DPP, DMP, PPP et PMP ainsi que le mode
prépayé/postpayé.
En prépaiement, le prix de la troisième tranche est celui de la deuxième.
Le compteur doit avoir une catégorie et un mode de paiement vérifiés.
La période de référence des tranches n'étant pas confirmée, aucun seuil
courant n'est calculé. Le montant FCFA d'une recharge ne permet pas de déduire
les kWh crédités sans connaître les retenues du ticket; seuls les kWh inscrits
sur le ticket doivent être enregistrés comme crédit, distincts de la consommation.
Une notification est émise à cinq kWh de solde ou moins, ou à trois jours
d'autonomie estimés ou moins. Les alertes d'approche de tranche restent
suspendues tant que la période de référence et ses règles ne sont pas validées.

La publication d'une facture exige désormais les deux bornes extraites de sa
période. La comparaison porte uniquement sur les factures SENELEC revues; un
reçu Woyofal n'est pas traité comme une facture de consommation. Le backend
nécessite un compteur attribué, un compteur unique sur le site, un unique
capteur ENERGY applicable, des index
cumulatifs encadrant la période à un jour près et une couverture temporelle
suffisante. Un capteur simulé produit un scénario distinct; plusieurs compteurs,
plusieurs capteurs réels ou des données insuffisantes n'entraînent aucune
comparaison agrégée.
Une comparaison n'est signalée qu'à partir de 15 % d'écart. Une alerte de
progression d'objectif n'est évaluée qu'après 30 % de sa durée écoulée et si la
mesure est en retard d'au moins 15 % de la cible par rapport à une trajectoire
linéaire indicative.

Les seuils du barème ne sont pas proratisés pour une facture dont la période
diffère de 30 jours : la règle de proratisation correspondante reste à confirmer
auprès de SENELEC. Les montants affichés dans les barèmes concernent uniquement
la composante énergie; aucune taxe, redevance ou retenue non confirmée n'est
ajoutée.
