import secrets
from datetime import datetime, time, timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from accounts.models import PreferencesUtilisateur, Utilisateur
from analysis.models import (
    Action,
    Anomalie,
    Decision,
    MemoireStrategique,
    ObservationOperationnelle,
    Recommandation,
    ResultatMetrique,
)
from billing.models import Plan
from billing.services import BillingAccessService, SubscriptionService
from energy.models import DonneeEnergetique, Objectif, SourceDonnee
from energy_assets.anomalies import code_metrique_site
from energy_assets.models import (
    Capteur,
    CapteurPresence,
    CommandeEquipement,
    DecisionAutonome,
    Equipement,
    EtatEquipement,
    MesureCapteur,
    MesurePresence,
    PilotageAutonome,
    ProfilFonctionnement,
    Zone,
)
from organizations.models import Compteur, Organisation, Site, UtilisateurOrganisation


SCENARIOS = (
    {
        "key": "multisite",
        "organisation": "Demo professionnelle — Groupe multisite",
        "secteur": "Services multi-sites",
        "email": "direction.multisite@demo.ecoscan.test",
        "nom": "Direction multisite EcoScan Demo",
        "sites": (
            ("Siège administratif — Dakar", ("Bureaux", "Salle informatique")),
            ("Agence régionale — Thiès", ("Accueil", "Bureaux")),
            ("Centre logistique — Saint-Louis", ("Entrepôt", "Quai")),
        ),
        "equipements": (
            ("Climatisation centrale", "CLIMATISATION", "8.500", "0.350", 1, "HAUTE", "ON"),
            ("Éclairage LED", "ÉCLAIRAGE", "1.200", "0.080", 1, "MOYENNE", "ON"),
            ("Serveur informatique", "INFORMATIQUE", "0.900", "0.120", 1, "CRITIQUE", "ON"),
            ("Pompe de réserve", "POMPE", "1.800", "0.050", 1, "BASSE", "OFF"),
        ),
    },
    {
        "key": "hotel",
        "organisation": "Demo professionnelle — Hôtel Baobab",
        "secteur": "Hôtellerie",
        "email": "direction.hotel@demo.ecoscan.test",
        "nom": "Direction Hôtel Baobab Demo",
        "sites": (("Hôtel Baobab — Dakar", ("Hébergement", "Cuisine", "Espaces communs")),),
        "equipements": (
            ("Centrale de climatisation", "CLIMATISATION", "18.000", "1.200", 1, "HAUTE", "ON"),
            ("Chambre froide cuisine", "REFRIGERATION", "5.500", "0.450", 1, "CRITIQUE", "ON"),
            ("Pompe eau chaude sanitaire", "POMPE", "2.400", "0.100", 2, "MOYENNE", "ON"),
            ("Éclairage des espaces communs", "ÉCLAIRAGE", "2.800", "0.180", 1, "MOYENNE", "ON"),
            ("Ascenseur de service", "MOTEUR", "4.200", "0.100", 1, "HAUTE", "OFF"),
        ),
    },
    {
        "key": "usine",
        "organisation": "Demo professionnelle — Industries Sahel",
        "secteur": "Industrie manufacturière",
        "email": "energie.usine@demo.ecoscan.test",
        "nom": "Responsable énergie Industries Sahel Demo",
        "sites": (("Usine de production — Diamniadio", ("Ligne de production", "Utilités", "Bureaux")),),
        "equipements": (
            ("Compresseur d'air industriel", "COMPRESSEUR", "22.000", "2.000", 1, "HAUTE", "ON"),
            ("Moteur ligne de production", "MOTEUR", "15.000", "0.800", 2, "CRITIQUE", "ON"),
            ("Groupe de froid process", "REFRIGERATION", "12.500", "1.000", 1, "HAUTE", "ON"),
            ("Pompe de circulation", "POMPE", "3.800", "0.250", 2, "MOYENNE", "OFF"),
            ("Éclairage atelier", "ÉCLAIRAGE", "4.600", "0.300", 1, "MOYENNE", "ON"),
        ),
    },
    {
        "key": "restaurant",
        "organisation": "Demo professionnelle — Restaurant Teranga",
        "secteur": "Restauration",
        "email": "gerance.restaurant@demo.ecoscan.test",
        "nom": "Gérance Restaurant Teranga Demo",
        "sites": (("Restaurant Teranga — Plateau, Dakar", ("Cuisine", "Salle", "Réserve")),),
        "equipements": (
            ("Armoire réfrigérée cuisine", "REFRIGERATION", "2.800", "0.240", 2, "HAUTE", "ON"),
            ("Extraction de cuisine", "VENTILATION", "3.200", "0.180", 1, "MOYENNE", "ON"),
            ("Climatisation salle", "CLIMATISATION", "6.500", "0.300", 1, "MOYENNE", "ON"),
            ("Éclairage salle et terrasse", "ÉCLAIRAGE", "1.700", "0.090", 1, "BASSE", "ON"),
            ("Lave-vaisselle professionnel", "EQUIPEMENT_CUISINE", "4.000", "0.200", 1, "MOYENNE", "OFF"),
        ),
    },
)


def _get_plan():
    plan = Plan.objects.filter(code="standard", actif=True).first()
    if plan is None:
        plan = Plan.objects.filter(actif=True).order_by("prix_mensuel").first()
    if plan is None:
        raise CommandError(
            "Aucun plan actif n’est disponible. Initialisez d’abord le catalogue billing."
        )
    return plan


def _create_measurements(sensor, power_kw, start, end, interval_hours=6):
    if MesureCapteur.objects.filter(capteur=sensor).exists():
        return

    timestamps = []
    cursor = start
    while cursor < end:
        timestamps.append(cursor)
        cursor += timedelta(hours=interval_hours)
    timestamps.append(end)
    index_value = Decimal("100")
    previous = None
    for measured_at in timestamps:
        if previous is not None:
            index_value += power_kw * Decimal(
                str((measured_at - previous).total_seconds() / 3600)
            )
        value = power_kw if sensor.type.upper() == "POWER" else index_value
        unit = "kW" if sensor.type.upper() == "POWER" else "kWh"
        MesureCapteur.objects.update_or_create(
            capteur=sensor,
            date_mesure=measured_at,
            defaults={"valeur": value, "unite": unit},
        )
        previous = measured_at


class Command(BaseCommand):
    help = (
        "Prépare des comptes et des données métier synthétiques pour les scénarios "
        "multisite, hôtel, usine et restaurant."
    )

    @transaction.atomic
    def handle(self, *args, **options):
        now = timezone.now().replace(second=0, microsecond=0)
        start = now - timedelta(days=28)
        plan = _get_plan()
        generated_credentials = []
        counts = {"organisations": 0, "sites": 0, "equipements": 0, "capteurs": 0}

        for scenario in SCENARIOS:
            organisation, created = Organisation.objects.get_or_create(
                nom=scenario["organisation"],
                defaults={
                    "secteur": scenario["secteur"],
                    "localisation": "Sénégal",
                    "statut": Organisation.Statut.ACTIVE,
                    "type_compte": "COMPLET",
                    "details_demande": {"ecoscan_demo_scenario": scenario["key"]},
                },
            )
            if created:
                counts["organisations"] += 1
            elif organisation.details_demande.get("ecoscan_demo_scenario") != scenario["key"]:
                raise CommandError(
                    f"L’organisation « {organisation.nom} » existe déjà hors du jeu de démonstration."
                )
            organisation.secteur = scenario["secteur"]
            organisation.localisation = "Sénégal"
            organisation.statut = Organisation.Statut.ACTIVE
            organisation.save(update_fields=("secteur", "localisation", "statut"))

            user = Utilisateur.objects.filter(email=scenario["email"]).first()
            if user is None:
                password = secrets.token_urlsafe(24)
                user = Utilisateur.objects.create_user(
                    email=scenario["email"],
                    password=password,
                    nom=scenario["nom"],
                    role=Utilisateur.Role.ADMIN_ORGANISATION,
                    actif=True,
                    is_staff=False,
                )
                generated_credentials.append((scenario["email"], password))
            elif user.organisations_membres.exclude(organisation=organisation).exists():
                raise CommandError(
                    f"Le compte « {scenario['email']} » est déjà rattaché à une autre organisation."
                )
            else:
                user.nom = scenario["nom"]
                user.role = Utilisateur.Role.ADMIN_ORGANISATION
                user.actif = True
                user.is_staff = False
                user.save(update_fields=("nom", "role", "actif", "is_staff"))
            UtilisateurOrganisation.objects.get_or_create(
                organisation=organisation,
                utilisateur=user,
            )
            PreferencesUtilisateur.objects.get_or_create(utilisateur=user)

            if BillingAccessService().abonnement_courant(organisation) is None:
                SubscriptionService().demarrer_essai_gratuit(organisation, plan)

            source, _ = SourceDonnee.objects.update_or_create(
                organisation=organisation,
                nom="Télémétrie synthétique EcoScan Demo",
                defaults={
                    "type": "CAPTEUR_SIMULE",
                    "origine": "Générateur de scénario local EcoScan",
                    "frequence": "Toutes les 6 heures (données historiques)",
                    "statut_synchronisation": "SYNCHRONISE",
                    "derniere_synchronisation": now,
                },
            )
            objectif, _ = Objectif.objects.update_or_create(
                organisation=organisation,
                nom="Réduire la consommation hors activité",
                defaults={
                    "description": (
                        "Objectif de démonstration : identifier et suivre les consommations "
                        "évitables hors horaires d’activité."
                    ),
                    "type": "ENERGIE",
                    "valeur_cible": Decimal("120.000000"),
                    "unite": "kWh",
                    "date_debut": now - timedelta(days=14),
                    "date_fin": now + timedelta(days=45),
                    "progression_actuelle": Decimal("32.000000"),
                    "prevision": Decimal("80.000000"),
                    "statut": Objectif.Statut.ACTIF,
                },
            )

            scenario_sites = []
            for site_index, (site_name, zone_names) in enumerate(scenario["sites"]):
                site, site_created = Site.objects.update_or_create(
                    organisation=organisation,
                    nom=site_name,
                    defaults={
                        "adresse": f"{site_name}, Sénégal",
                        "pays": "Sénégal",
                        "fuseau_horaire": "Africa/Dakar",
                    },
                )
                counts["sites"] += int(site_created)
                zones = []
                for zone_name in zone_names:
                    zone, _ = Zone.objects.update_or_create(
                        site=site,
                        nom=zone_name,
                        defaults={
                            "description": (
                                f"Zone de démonstration du scénario {scenario['key']}."
                            ),
                            "active": True,
                        },
                    )
                    zones.append(zone)
                scenario_sites.append((site, zones, site_index))

            all_site_equipment = []
            for site, zones, site_index in scenario_sites:
                site_equipments = []
                for equipment_index, spec in enumerate(scenario["equipements"]):
                    name, category, nominal, standby, quantity, criticality, state = spec
                    zone = zones[equipment_index % len(zones)]
                    equipment, equipment_created = Equipement.objects.update_or_create(
                        site=site,
                        nom=name,
                        defaults={
                            "zone": zone,
                            "categorie": category,
                            "puissance_nominale_kw": Decimal(nominal),
                            "puissance_veille_kw": Decimal(standby),
                            "quantite": quantity,
                            "etat_operationnel": state,
                            "criticite": criticality,
                            "monitoring_active": True,
                        },
                    )
                    counts["equipements"] += int(equipment_created)
                    online = state == Equipement.Etat.ON
                    current_power = (
                        Decimal(nominal) * quantity
                        if online
                        else Decimal(standby) * quantity
                    )
                    EtatEquipement.objects.update_or_create(
                        equipement=equipment,
                        defaults={
                            "etat_souhaite": state,
                            "etat_rapporte": state,
                            "puissance_actuelle_kw": current_power,
                            "energie_cumulee_kwh": Decimal("100"),
                            "statut_synchronisation": (
                                EtatEquipement.StatutSynchronisation.SYNCHRONIZED
                            ),
                            "date_etat_souhaite": now,
                            "date_etat_rapporte": now,
                        },
                    )

                    for sensor_type in ("POWER", "ENERGY"):
                        sensor_identifier = (
                            f"ECOSCAN-DEMO-{scenario['key']}-{site_index}-"
                            f"{equipment_index}-{sensor_type}"
                        )
                        sensor, sensor_created = Capteur.objects.update_or_create(
                            identifiant=sensor_identifier,
                            defaults={
                                "equipement": equipment,
                                "type": sensor_type,
                                "mode": Capteur.Mode.SIMULATED,
                                "frequence_secondes": 15,
                                "statut": Capteur.Statut.ACTIVE,
                                "derniere_communication": now,
                            },
                        )
                        counts["capteurs"] += int(sensor_created)
                        sensor_power = (
                            Decimal(nominal) * quantity if online else Decimal(standby) * quantity
                        )
                        _create_measurements(sensor, sensor_power, start, now)
                    site_equipments.append(equipment)

                    if category == "CLIMATISATION":
                        for weekday in range(7):
                            ProfilFonctionnement.objects.update_or_create(
                                equipement=equipment,
                                jour_semaine=weekday,
                                heure_debut=time(7, 0),
                                defaults={
                                    "nom": "Plage d'activité du scénario",
                                    "heure_fin": time(23, 0),
                                    "actif": True,
                                },
                            )

                all_site_equipment.append((site, zones, site_equipments))

                presence_sensor, _ = CapteurPresence.objects.update_or_create(
                    identifiant=(
                        f"ECOSCAN-DEMO-{scenario['key']}-{site_index}-PRESENCE"
                    ),
                    defaults={
                        "zone": zones[0],
                        "mode": CapteurPresence.Mode.SIMULATED,
                        "occupation_simulee": True,
                        "frequence_secondes": 15,
                        "statut": CapteurPresence.Statut.ACTIVE,
                        "derniere_communication": now,
                    },
                )
                for sample_index in range(29):
                    measured_at = start + timedelta(days=sample_index, hours=12)
                    MesurePresence.objects.update_or_create(
                        capteur=presence_sensor,
                        date_mesure=measured_at,
                        defaults={"occupee": sample_index % 5 != 0},
                    )
                MesurePresence.objects.update_or_create(
                    capteur=presence_sensor,
                    date_mesure=now,
                    defaults={"occupee": True},
                )

                payment_mode = (
                    Compteur.ModePaiement.PREPAYE
                    if scenario["key"] == "restaurant" or (
                        scenario["key"] == "multisite" and site_index == 1
                    )
                    else Compteur.ModePaiement.POSTPAYE
                )
                meter, _ = Compteur.objects.update_or_create(
                    reference=f"ECOSCAN-DEMO-{scenario['key']}-{site_index}-METER",
                    defaults={
                        "site": site,
                        "type_energie": "ELECTRICITE",
                        "unite": "kWh",
                        "localisation": "Compteur de démonstration",
                        "statut_synchronisation": "SYNCHRONISE",
                        "derniere_synchronisation": now,
                        "puissance_souscrite_kva": Decimal("80.00"),
                        "categorie_tarifaire": Compteur.CategorieTarifaire.PMP,
                        "mode_paiement": payment_mode,
                    },
                )
                index_value = Decimal("1000.000000")
                for day_offset in range(28, -1, -1):
                    reading_day = timezone.localdate(now) - timedelta(days=day_offset)
                    index_value += Decimal("45.000000") + Decimal(
                        (reading_day.day % 5) * 3
                    )
                    reading_start = timezone.make_aware(
                        datetime.combine(reading_day, time.min),
                        timezone.get_current_timezone(),
                    )
                    reading_end = min(
                        reading_start + timedelta(days=1),
                        now,
                    )
                    if reading_end <= reading_start:
                        continue
                    DonneeEnergetique.objects.update_or_create(
                        compteur=meter,
                        date_releve=reading_day,
                        creneau="journalier",
                        defaults={
                            "source_donnee": source,
                            "valeur": index_value,
                            "unite": "kWh",
                            "periode_debut": reading_start,
                            "periode_fin": reading_end,
                            "statut_validation": DonneeEnergetique.StatutValidation.VALIDEE,
                            "source": "ECOSCAN_SYNTHETIC_DEMO",
                        },
                    )

                metric, _ = ResultatMetrique.objects.update_or_create(
                    organisation=organisation,
                    compteur=meter,
                    code_metrique=code_metrique_site(site),
                    periode_debut=start,
                    periode_fin=now,
                    version_metrique="demo-v1",
                    defaults={
                        "valeur": Decimal("18.500000"),
                        "unite": "%",
                        "baseline_type": "moyenne_historique_synthetique",
                        "baseline_valeur": Decimal("15.600000"),
                        "baseline_nombre_observations": 4,
                        "completude": Decimal("1.0000"),
                        "statut_qualite": ResultatMetrique.StatutQualite.ESTIME,
                        "confiance": Decimal("0.7500"),
                        "sources": [{"source": "ECOSCAN_SYNTHETIC_DEMO"}],
                        "limites": ["Données synthétiques de démonstration."],
                    },
                )
                anomaly, _ = Anomalie.objects.update_or_create(
                    organisation=organisation,
                    resultat_metrique=metric,
                    type="CONSOMMATION_HORS_HORAIRES",
                    defaults={
                        "severite": Anomalie.Severite.ALERTE,
                        "valeur_observee": Decimal("18.500000"),
                        "valeur_attendue": Decimal("15.600000"),
                        "ecart_pourcentage": Decimal("18.59"),
                        "statut": Anomalie.Statut.DETECTED,
                    },
                )

                objective_recommendation, _ = Recommandation.objects.update_or_create(
                    objectif=objectif,
                    titre=f"Vérifier les usages hors horaires — {site.nom}",
                    defaults={
                        "anomalie": anomaly,
                        "description": (
                            "Cas de démonstration : examiner les consommations hors "
                            "horaires avant toute action opérationnelle."
                        ),
                        "impact_estime": Decimal("24.000000"),
                        "economie_estimee": Decimal("24.00"),
                        "unite": "kWh",
                        "priorite": Recommandation.Priorite.HAUTE,
                        "statut": Recommandation.Statut.PROPOSEE,
                        "date_echeance": now + timedelta(days=7),
                    },
                )
                action, _ = Action.objects.update_or_create(
                    recommandation=objective_recommendation,
                    titre=f"Contrôler les équipements de {site.nom}",
                    defaults={
                        "description": "Vérification terrain de démonstration, sans commande matérielle.",
                        "statut": Action.Statut.EN_COURS,
                        "responsable": user,
                        "equipement": site_equipments[0],
                        "date_echeance": now + timedelta(days=3),
                    },
                )
                ObservationOperationnelle.objects.update_or_create(
                    organisation=organisation,
                    texte=f"Observation synthétique de consommation pour {site.nom}.",
                    defaults={
                        "auteur": user,
                        "date_observation": now - timedelta(hours=2),
                        "creneau": "DEMO",
                        "valide": False,
                    },
                )
                MemoireStrategique.objects.update_or_create(
                    organisation=organisation,
                    titre=f"Étude de consommation — {site.nom}",
                    defaults={
                        "anomalie": anomaly,
                        "action": action,
                        "signal_initial": "Écart synthétique de consommation hors horaires.",
                        "hypothese_texte": "Hypothèse de démonstration, à confirmer par un humain.",
                        "action_texte": "Contrôle des horaires et de l'état rapporté.",
                        "impact_attendu_fcfa": Decimal("2400.00"),
                        "statut": MemoireStrategique.Statut.A_VERIFIER,
                        "sources": [{"source": "ECOSCAN_SYNTHETIC_DEMO"}],
                        "indexee_rag": False,
                    },
                )
                Decision.objects.update_or_create(
                    recommandation=objective_recommendation,
                    defaults={
                        "resultat": "À examiner",
                        "commentaire": "Décision synthétique non validée en environnement de démonstration.",
                        "date_decision": now,
                        "decideur": user,
                    },
                )

                policy, _ = PilotageAutonome.objects.update_or_create(
                    site=site,
                    defaults={
                        "actif": False,
                        "arret_urgence": False,
                        "plafond_puissance_kw": Decimal("40.000"),
                        "horaires_actifs": True,
                        "heure_debut": time(7, 0),
                        "heure_fin": time(23, 0),
                        "confort_actif": False,
                        "strategie_automatique": False,
                        "occupation_active": True,
                        "derniere_evaluation": now,
                    },
                )
                eligible = [
                    equipment
                    for equipment in site_equipments
                    if equipment.criticite in (
                        Equipement.Criticite.BASSE,
                        Equipement.Criticite.MOYENNE,
                    )
                ]
                policy.equipements_autorises.set(eligible[:2])
                policy.zones_autorisees.set(zones[:2])

                decision_equipment = site_equipments[0]
                decision_power = (
                    decision_equipment.puissance_nominale_kw
                    * decision_equipment.quantite
                    if decision_equipment.etat_operationnel == Equipement.Etat.ON
                    else decision_equipment.puissance_veille_kw
                    * decision_equipment.quantite
                )
                DecisionAutonome.objects.get_or_create(
                    site=site,
                    equipement=decision_equipment,
                    strategie=DecisionAutonome.Strategie.SYSTEM,
                    statut=DecisionAutonome.Statut.NO_ACTION,
                    raison="Aucune commande réelle : historique synthétique de démonstration.",
                    defaults={
                        "action": "",
                        "puissance_avant_kw": decision_power,
                        "puissance_apres_kw": decision_power,
                        "temperature_simulee_c": Decimal("24.00"),
                    },
                )
                CommandeEquipement.objects.get_or_create(
                    equipement=decision_equipment,
                    action=CommandeEquipement.Action.OFF,
                    statut=CommandeEquipement.Statut.FAILED,
                    demande_par=user,
                    defaults={
                        "date_envoi": now - timedelta(hours=1),
                        "date_finalisation": now - timedelta(hours=1),
                        "detail_echec": "Échec synthétique de démonstration; aucune passerelle appelée.",
                    },
                )

        self.stdout.write(
            self.style.SUCCESS(
                "Scénarios prêts : "
                f"{len(SCENARIOS)} organisations, {counts['sites']} sites ajoutés, "
                f"{counts['equipements']} équipements ajoutés, "
                f"{counts['capteurs']} capteurs simulés ajoutés."
            )
        )
        if generated_credentials:
            self.stdout.write("Identifiants temporaires (comptes nouvellement créés) :")
            for email, password in generated_credentials:
                self.stdout.write(f"{email} | {password}")
        else:
            self.stdout.write(
                "Aucun nouveau compte : les mots de passe existants n’ont pas été modifiés."
            )
        self.stdout.write(
            "Toutes les mesures et décisions créées sont synthétiques; "
            "les politiques d’autopilotage restent désactivées."
        )
