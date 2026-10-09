# Actions virtuelles

Les actions virtuelles estiment l'effet énergétique d'un changement d'état d'un
équipement dans le contexte d'une recommandation. Elles ne modifient ni l'état
souhaité/rapporté ni la file de commandes Home Assistant.

## API

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

## Estimation

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
