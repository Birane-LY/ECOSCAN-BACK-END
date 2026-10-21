from datetime import datetime, time, timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from accounts.models import PreferencesUtilisateur, Utilisateur
from analysis.models import Action, Recommandation
from billing.models import Plan
from billing.services import BillingAccessService, SubscriptionService
from energy.models import DonneeEnergetique, Objectif, SourceDonnee
from energy_assets.models import (
    Capteur,
    Equipement,
    EtatEquipement,
    MesureCapteur,
    ProfilFonctionnement,
    Zone,
)
from organizations.models import Compteur, Organisation, Site, UtilisateurOrganisation


ORGANISATION_NOM = "EcoScan Démonstration Énergétique"
COMPTES = (
    ("pilot.ecoscan@example.test", "Pilote EcoScan", Utilisateur.Role.ADMIN_ORGANISATION),
    ("gestionnaire.ecoscan@example.test", "Gestionnaire EcoScan", Utilisateur.Role.UTILISATEUR_ORGANISATION),
    ("consultant.ecoscan@example.test", "Consultant EcoScan", Utilisateur.Role.CONSULTANT),
)
SITES = (
    ("Siège — Plateau", "Avenue Léopold Sédar Senghor, Dakar"),
    ("Centre de services — Almadies", "Route des Almadies, Dakar"),
)
EQUIPEMENTS = (
    (0, 0, "Climatisation salle de réunion", "CLIMATISATION", "3.500", "0.080", 1, True, Equipement.Criticite.HAUTE),
    (0, 0, "Climatisation open space", "CLIMATISATION", "2.800", "0.060", 2, True, Equipement.Criticite.HAUTE),
    (0, 1, "Éclairage des bureaux", "ÉCLAIRAGE", "0.420", "0.015", 8, True, Equipement.Criticite.MOYENNE),
    (0, 1, "Serveur informatique", "INFORMATIQUE", "0.650", "0.025", 1, True, Equipement.Criticite.CRITIQUE),
    (0, 0, "Écran du hall d’accueil", "AUDIOVISUEL", "0.180", "0.008", 1, False, Equipement.Criticite.BASSE),
    (1, 0, "Climatisation espace client", "CLIMATISATION", "2.400", "0.050", 1, True, Equipement.Criticite.HAUTE),
    (1, 0, "Éclairage de l’accueil", "ÉCLAIRAGE", "0.300", "0.012", 6, True, Equipement.Criticite.MOYENNE),
    (1, 1, "Pompe de relevage", "POMPE", "0.850", "0.020", 1, False, Equipement.Criticite.MOYENNE),
    (1, 1, "Postes informatiques", "INFORMATIQUE", "0.220", "0.010", 5, True, Equipement.Criticite.MOYENNE),
)


class Command(BaseCommand):
    help = "Crée ou actualise un jeu de données pilote EcoScan sans toucher aux autres organisations."

    def add_arguments(self, parser):
        parser.add_argument(
            "--password",
            required=True,
            help="Mot de passe temporaire partagé par les trois comptes pilotes.",
        )

    @transaction.atomic
    def handle(self, *args, **options):
        password = options["password"]
        if len(password) < 20:
            raise CommandError("Le mot de passe pilote doit contenir au moins 20 caractères.")

        organisation, _ = Organisation.objects.get_or_create(
            nom=ORGANISATION_NOM,
            defaults={
                "secteur": "Services et bureaux",
                "localisation": "Dakar, Sénégal",
                "statut": Organisation.Statut.ACTIVE,
                "type_compte": "COMPLET",
            },
        )
        organisation.secteur = "Services et bureaux"
        organisation.localisation = "Dakar, Sénégal"
        organisation.statut = Organisation.Statut.ACTIVE
        organisation.save(
            update_fields=("secteur", "localisation", "statut")
        )

        users = []
        for email, nom, role in COMPTES:
            user, created = Utilisateur.objects.get_or_create(
                email=email,
                defaults={"nom": nom, "role": role, "actif": True},
            )
            if (
                not created
                and user.organisations_membres.exclude(
                    organisation=organisation
                ).exists()
            ):
                raise CommandError(
                    f"Le compte pilote {email} est déjà rattaché à une autre organisation."
                )
            user.nom = nom
            user.role = role
            user.actif = True
            user.is_staff = False
            user.set_password(password)
            user.save(
                update_fields=(
                    "nom",
                    "role",
                    "actif",
                    "is_staff",
                    "password",
                    "mot_de_passe_hash",
                )
            )
            UtilisateurOrganisation.objects.get_or_create(
                organisation=organisation,
                utilisateur=user,
            )
            PreferencesUtilisateur.objects.get_or_create(utilisateur=user)
            users.append(user)

        sites = []
        for nom, adresse in SITES:
            site, _ = Site.objects.update_or_create(
                organisation=organisation,
                nom=nom,
                defaults={
                    "adresse": adresse,
                    "pays": "Sénégal",
                    "fuseau_horaire": "Africa/Dakar",
                },
            )
            sites.append(site)

        zones_by_site = []
        for site, zone_names in zip(
            sites,
            (("Bureaux", "Locaux techniques"), ("Accueil", "Espaces communs")),
        ):
            zones = []
            for nom in zone_names:
                zone, _ = Zone.objects.update_or_create(
                    site=site,
                    nom=nom,
                    defaults={"description": f"Zone suivie du site {site.nom}."},
                )
                zones.append(zone)
            zones_by_site.append(zones)

        now = timezone.now().replace(second=0, microsecond=0)
        start_day = timezone.localdate(now) - timedelta(days=2)
        start = timezone.make_aware(
            datetime.combine(start_day, time.min),
            timezone.get_current_timezone(),
        )
        full_hours = int((now - start).total_seconds() // 3600)
        timestamps = [start + timedelta(hours=index) for index in range(full_hours + 1)]
        if timestamps[-1] != now:
            timestamps.append(now)

        equipment_rows = []
        for site_index, zone_index, nom, categorie, nominal, standby, quantity, on, criticality in EQUIPEMENTS:
            site = sites[site_index]
            equipment, _ = Equipement.objects.update_or_create(
                site=site,
                nom=nom,
                defaults={
                    "zone": zones_by_site[site_index][zone_index],
                    "categorie": categorie,
                    "puissance_nominale_kw": Decimal(nominal),
                    "puissance_veille_kw": Decimal(standby),
                    "quantite": quantity,
                    "etat_operationnel": (
                        Equipement.Etat.ON if on else Equipement.Etat.OFF
                    ),
                    "criticite": criticality,
                    "monitoring_active": True,
                },
            )
            equipment_rows.append((equipment, on))

            state_power = Decimal(nominal) * quantity if on else Decimal("0")
            simulated_energy_power = (
                Decimal(nominal) * quantity if on else Decimal(standby) * quantity
            )
            EtatEquipement.objects.update_or_create(
                equipement=equipment,
                defaults={
                    "etat_souhaite": (
                        Equipement.Etat.ON if on else Equipement.Etat.OFF
                    ),
                    "etat_rapporte": (
                        Equipement.Etat.ON if on else Equipement.Etat.OFF
                    ),
                    "puissance_actuelle_kw": state_power,
                    "statut_synchronisation": (
                        EtatEquipement.StatutSynchronisation.SYNCHRONIZED
                    ),
                    "date_etat_souhaite": now,
                    "date_etat_rapporte": now,
                },
            )

            power_sensor, _ = Capteur.objects.update_or_create(
                identifiant=f"ECOSCAN-PILOT-{equipment.id}-POWER",
                defaults={
                    "equipement": equipment,
                    "type": "POWER",
                    "mode": Capteur.Mode.SIMULATED,
                    "frequence_secondes": 15,
                    "statut": Capteur.Statut.ACTIVE,
                    "derniere_communication": now,
                },
            )
            energy_sensor, _ = Capteur.objects.update_or_create(
                identifiant=f"ECOSCAN-PILOT-{equipment.id}-ENERGY",
                defaults={
                    "equipement": equipment,
                    "type": "ENERGY",
                    "mode": Capteur.Mode.SIMULATED,
                    "frequence_secondes": 15,
                    "statut": Capteur.Statut.ACTIVE,
                    "derniere_communication": now,
                },
            )

            energy_index = Decimal("100.000000")
            previous_at = None
            for measured_at in timestamps:
                if previous_at is not None:
                    hours = Decimal(
                        str((measured_at - previous_at).total_seconds())
                    ) / Decimal("3600")
                    energy_index += simulated_energy_power * hours
                power = state_power
                MesureCapteur.objects.get_or_create(
                    capteur=power_sensor,
                    date_mesure=measured_at,
                    defaults={"valeur": power, "unite": "kW"},
                )
                MesureCapteur.objects.get_or_create(
                    capteur=energy_sensor,
                    date_mesure=measured_at,
                    defaults={"valeur": energy_index, "unite": "kWh"},
                )
                previous_at = measured_at

            equipment_rows[-1] = (equipment, on, energy_index, power_sensor, energy_sensor)

            if categorie == "CLIMATISATION":
                for weekday in range(7):
                    ProfilFonctionnement.objects.get_or_create(
                        equipement=equipment,
                        jour_semaine=weekday,
                        heure_debut=time(8, 0),
                        defaults={
                            "nom": "Plage d’ouverture du site",
                            "heure_fin": time(23, 0),
                            "actif": True,
                        },
                    )

        for index, site in enumerate(sites, start=1):
            Compteur.objects.update_or_create(
                reference=f"ECOSCAN-PILOT-METER-{index:02d}",
                defaults={
                    "site": site,
                    "type_energie": "ELECTRICITE",
                    "unite": "kWh",
                    "localisation": "Local compteur principal",
                    "statut_synchronisation": "SYNCHRONISE",
                    "derniere_synchronisation": now,
                    "puissance_souscrite_kva": Decimal("30.00"),
                },
            )

        source, _ = SourceDonnee.objects.get_or_create(
            organisation=organisation,
            nom="Télémétrie du site pilote",
            defaults={
                "type": "CAPTEUR_SIMULE",
                "origine": "Capteurs virtuels EcoScan",
                "frequence": "15 secondes",
                "statut_synchronisation": "SYNCHRONISE",
                "derniere_synchronisation": now,
            },
        )

        meters = Compteur.objects.filter(
            site__organisation=organisation,
            reference__startswith="ECOSCAN-PILOT-METER-",
        )
        for meter in meters:
            index_value = Decimal("1200.000000")
            for day_offset in range(29, -1, -1):
                reading_day = timezone.localdate(now) - timedelta(days=day_offset)
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
                daily_use = Decimal("18.500000") + Decimal(
                    (reading_day.day % 7) * 2
                )
                index_value += daily_use
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
                        "source": "ECOSCAN_PILOT",
                    },
                )

        plan = Plan.objects.filter(code="standard", actif=True).first()
        if plan is None:
            plan = Plan.objects.filter(actif=True).order_by("prix_mensuel").first()
        if plan is None:
            raise CommandError("Aucun plan actif n’est disponible pour le compte pilote.")
        if BillingAccessService().abonnement_courant(organisation) is None:
            SubscriptionService().demarrer_essai_gratuit(organisation, plan)

        objectif, _ = Objectif.objects.get_or_create(
            organisation=organisation,
            nom="Réduire les consommations hors horaires",
            defaults={
                "description": "Suivre la réduction mesurée de la consommation des équipements en dehors des heures d’ouverture.",
                "type": "ENERGIE",
                "valeur_cible": Decimal("80.000000"),
                "unite": "kWh",
                "date_debut": now - timedelta(days=30),
                "date_fin": now + timedelta(days=60),
                "statut": Objectif.Statut.ACTIF,
            },
        )
        recommendation, _ = Recommandation.objects.get_or_create(
            objectif=objectif,
            titre="Vérifier l’arrêt des climatiseurs après la fermeture",
            defaults={
                "description": "Examiner les climatiseurs qui restent en marche après les heures d’ouverture et évaluer leur arrêt.",
                "impact_estime": Decimal("80.000000"),
                "economie_estimee": Decimal("80.00"),
                "unite": "kWh",
                "priorite": Recommandation.Priorite.HAUTE,
                "statut": Recommandation.Statut.PROPOSEE,
                "date_echeance": now + timedelta(days=7),
            },
        )
        target_ac = next(
            equipment
            for equipment, *_ in equipment_rows
            if equipment.categorie == "CLIMATISATION"
        )
        Action.objects.get_or_create(
            recommandation=recommendation,
            titre="Examiner le fonctionnement de la climatisation hors horaires",
            defaults={
                "description": "Contrôler les mesures du site pilote avant de décider d’un changement de consigne.",
                "statut": Action.Statut.A_FAIRE,
                "responsable": users[0],
                "equipement": target_ac,
                "date_echeance": now + timedelta(days=3),
            },
        )

        self.stdout.write(
            self.style.SUCCESS(
                "Jeu pilote prêt : "
                f"1 organisation, {len(sites)} sites, {len(users)} comptes, "
                f"{len(equipment_rows)} équipements, "
                f"{len(equipment_rows) * 2} capteurs, "
                f"{MesureCapteur.objects.filter(capteur__equipement__site__organisation=organisation).count()} mesures, "
                "2 compteurs et un essai actif."
            )
        )
        self.stdout.write(
            f"Compte administrateur : {COMPTES[0][0]} | "
            f"heure de référence : {timezone.localtime(now).isoformat()}"
        )
