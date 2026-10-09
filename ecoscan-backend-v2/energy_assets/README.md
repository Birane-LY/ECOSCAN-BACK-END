# Energy assets

## Capteur simulator

From the `ecoscan-backend-v2` directory, initialize or update the database
schema before the first run:

```powershell
python manage.py migrate
```

The simulator needs the V2 `energy_assets` tables created by these migrations.
Use the same Python environment and database configuration for migration and
simulation.

Run the simulator as a separate process alongside Django:

```powershell
python manage.py simuler_capteurs
```

The command checks active `SIMULATED` sensors once per second and emits a
measurement when each sensor's `frequence_secondes` interval has elapsed. Use
`python manage.py simuler_capteurs --once` to run one check, or
`--interval 2` to change the polling interval. The polling interval does not
change each sensor's configured measurement frequency.

`POWER` sensors report the equipment's nominal power while it is on and its
standby power while it is off. The simulator follows the desired state first,
then the reported state; if both are unknown, it starts the simulated equipment
as on. `ENERGY` sensors integrate the simulated power into a cumulative kWh
reading. Unsupported sensor types and real sensors are left untouched.

The generated measurements use the regular telemetry synchronization service,
so they update equipment state, cumulative energy, and sensor communication
timestamps in the same way as ingested readings. The simulator does not send
commands to physical equipment.

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
