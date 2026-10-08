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

Depuis ce dossier, copiez `.env.example` vers `.env` et configurez les URL et
secrets. Copiez `equipment-entities.example.json` vers
`equipment-entities.json`, puis remplacez l'exemple par un mapping des UUID
d'équipements EcoScan vers leurs entités actionneurs Home Assistant :

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

Après avoir copié et renseigné `.env` et le mapping, chargez les variables et
lancez l'agent depuis ce dossier :

```powershell
Copy-Item .env.example .env
Copy-Item equipment-entities.example.json equipment-entities.json
# Renseigner les valeurs réelles dans les deux fichiers avant de continuer.
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
