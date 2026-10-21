# Passerelle locale Django - Home Assistant

Cette passerelle exécute les commandes ON/OFF des équipements V2 sans exposer
les API internes de Django sur Internet :

1. Elle interroge périodiquement `POST
   /api/internal/energy-assets/commandes/suivante/` avec
   `X-Internal-Service-Token`.
2. Elle mappe l'identifiant UUID de l'équipement EcoScan vers une entité
   actionneur Home Assistant et appelle `homeassistant.turn_on` ou
   `homeassistant.turn_off` avec un jeton Bearer.
3. Elle lit l'état de l'entité Home Assistant. Seul l'état `on`/`off` observé
   correspondant à l'action demandée confirme la commande dans Django.
4. En cas d'erreur, elle signale la commande en échec. Les accusés de réception
   dont l'envoi échoue sont persistés dans une file locale et retentés avant de
   récupérer de nouvelles commandes.

## Prérequis et configuration

- Python 3.10 ou ultérieur ; l'agent utilise uniquement la bibliothèque standard.
- Une instance Home Assistant joignable depuis la machine passerelle.
- Un jeton d'accès longue durée Home Assistant. Créez un compte dédié et
  limitez ses droits autant que le permet votre version de Home Assistant.
- `DJANGO_INTERNAL_TOKEN` identique au secret Django. La passerelle et Django
  doivent communiquer par HTTPS ou sur un réseau privé/VPN de confiance. Ne
  publiez jamais les routes `/api/internal/` sur Internet.

### Home Assistant local de développement

Le fichier `docker-compose.home-assistant.yml` lance une instance Home
Assistant locale pour le développement. Sa configuration est persistée dans
`home-assistant/config/` et l'interface est disponible sur
`http://localhost:8123`. Cette instance est distincte de toute installation
Home Assistant existante et ne découvre pas les équipements connectés à un
autre hôte.

Depuis PowerShell, à la racine `ecoscan-backend-v2` :

```powershell
docker compose --file .\docker-compose.home-assistant.yml up -d
```

Terminez l'assistant de configuration dans le navigateur, puis créez un jeton
d'accès longue durée depuis votre profil Home Assistant. Renseignez ce jeton
dans `gateway/.env` (`HOME_ASSISTANT_TOKEN`) avant de lancer la passerelle. Le
jeton n'est pas inclus dans le dépôt.

Depuis `ecoscan-backend-v2`, copiez `gateway/.env.example` vers `gateway/.env`
et configurez les URL et secrets. Copiez
`gateway/equipment-entities.example.json` vers
`gateway/equipment-entities.json`, puis remplacez l'exemple par un mapping des
UUID d'équipements EcoScan vers leurs entités actionneurs Home Assistant :

```json
{
  "6a8354e0-6088-4979-9bc9-f2055d2d7401": "switch.pompe_salle_technique"
}
```

Utilisez uniquement des entités contrôlables. L'état Home Assistant confirme
le retour déclaré par le contrôleur, pas nécessairement le passage effectif du
courant : une preuve physique nécessite un retour d'état câblé sur le relais ou
le contacteur. Les capteurs de puissance restent nécessaires pour confirmer la
consommation électrique réelle.

Le profil de démonstration « Atelier et fournil — Dakar » utilise cinq
`input_boolean` Home Assistant associés aux équipements EcoScan dans le fichier
d'exemple. Ces interrupteurs sont virtuels : ils valident le parcours ON/OFF,
mais ne commandent aucun appareil physique.

### Exécution dans Docker Desktop sous Windows

Le fichier `docker-compose.gateway.yml` démarre la passerelle comme un service
long terme sans publier de port. Il utilise les noms de service
`http://backend:8000` et `http://home-assistant:8123` sur le réseau Compose
partagé. Démarrez le backend, Home Assistant et la passerelle dans le même
projet Compose. Si le backend ou Home Assistant se trouve sur un autre hôte ou
réseau, adaptez `DJANGO_BASE_URL` et `HOME_ASSISTANT_URL` à des adresses
joignables depuis la passerelle. Configurez le même jeton interne que Django,
un jeton Home Assistant à privilèges limités et le mapping avant le démarrage.

Depuis PowerShell, à la racine `ecoscan-backend-v2` :

```powershell
Copy-Item .\gateway\.env.example .\gateway\.env
Copy-Item .\gateway\equipment-entities.example.json .\gateway\equipment-entities.json
# Configurer les vrais secrets et mappings avant de démarrer la passerelle.
docker compose --file .\docker-compose.gateway.yml config --quiet
docker compose --file .\docker-compose.gateway.yml up --build -d
docker compose --file .\docker-compose.gateway.yml logs --follow home-assistant-gateway
```

Arrêtez le service avec
`docker compose --file .\docker-compose.gateway.yml down`. La file d'accusés de
réception en attente est conservée dans le volume Docker `gateway_outbox`.
N'utilisez pas le mapping d'exemple pour piloter du matériel.

Pour un lancement Python natif, depuis le dossier `ecoscan-backend-v2\gateway`
après avoir renseigné `.env` et `equipment-entities.json` :

```powershell
$envFile = Get-Content .env | Where-Object { $_ -match '^[A-Za-z_][A-Za-z0-9_]*=' }
foreach ($line in $envFile) {
    $parts = $line -split '=', 2
    [Environment]::SetEnvironmentVariable($parts[0], $parts[1], 'Process')
}
python home_assistant_agent.py
```

Pour le lancement permanent, utilisez un service supervisé et conservez
`GATEWAY_OUTBOX_PATH` sur un disque persistant. Ne versionnez ni `.env`, ni
`equipment-entities.json`, ni le répertoire `data/`.

## Sécurité et essais

- Commencez par un actionneur de test non critique et contrôlé localement.
- Les appels Home Assistant sont suivis d'une lecture de l'état ; en l'absence
  de retour correspondant sous `HOME_ASSISTANT_STATE_TIMEOUT`, la commande est
  déclarée en échec et l'état rapporté dans EcoScan n'est pas confirmé.
- Les opérations électriques doivent être installées et vérifiées par un
  professionnel qualifié, avec des relais/contacteurs adaptés à la charge et
  un arrêt manuel indépendant. Ne pilotez pas directement une charge secteur
  avec une carte de développement.
- Des tests unitaires hors matériel sont disponibles avec
  `python -m unittest gateway.test_home_assistant_agent`.

Le processus n'automatise ni la découverte des équipements ni leur association
à des entités Home Assistant : validez explicitement chaque mapping avant
d'activer le pilotage à distance.
