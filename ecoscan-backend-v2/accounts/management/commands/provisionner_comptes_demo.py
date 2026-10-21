import os

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from accounts.models import PreferencesUtilisateur, Utilisateur
from organizations.models import Organisation, UtilisateurOrganisation


COMPTES_DEMO = (
    (
        "mobile.commande.test@ecoscan.invalid",
        "Compte pilote EcoScan",
        Utilisateur.Role.ADMIN_ORGANISATION,
        "ECOSCAN_DEMO_CONTROL_PASSWORD",
    ),
    (
        "pilote.multisite@ecoscan.invalid",
        "Compte multisite EcoScan",
        Utilisateur.Role.ADMIN_ORGANISATION,
        "ECOSCAN_DEMO_MULTISITE_PASSWORD",
    ),
    (
        "pilote.anomalies@ecoscan.invalid",
        "Compte anomalies EcoScan",
        Utilisateur.Role.ADMIN_ORGANISATION,
        "ECOSCAN_DEMO_ANOMALIES_PASSWORD",
    ),
    (
        "demo.lecture@demo.ecoscan.test",
        "Compte lecture EcoScan",
        Utilisateur.Role.CONSULTANT,
        "ECOSCAN_DEMO_LECTURE_PASSWORD",
    ),
)


class Command(BaseCommand):
    help = "Provisionne les comptes de démonstration depuis des secrets d'environnement."

    @transaction.atomic
    def handle(self, *args, **options):
        passwords = {}
        for email, _name, _role, variable in COMPTES_DEMO:
            password = os.environ.get(variable, "")
            if len(password) < 20:
                raise CommandError(
                    f"{variable} must be configured with at least 20 characters."
                )
            passwords[email] = password

        if len(set(passwords.values())) != len(passwords):
            raise CommandError("Each demo account must have a distinct password.")

        organisations_pilotes = []
        for email, _name, _role, _variable in COMPTES_DEMO[:3]:
            try:
                user = Utilisateur.objects.get(email=email)
            except Utilisateur.DoesNotExist as error:
                raise CommandError(
                    f"Demo account {email} is missing from the demo database."
                ) from error
            memberships = list(
                UtilisateurOrganisation.objects.filter(utilisateur=user)
                .select_related("organisation")
            )
            if len(memberships) != 1:
                raise CommandError(
                    f"Demo account {email} must belong to exactly one organization."
                )
            organisations_pilotes.append(memberships[0].organisation)

        if len({organisation.pk for organisation in organisations_pilotes}) != 3:
            raise CommandError(
                "The pilot demo accounts must belong to three distinct organizations."
            )

        for email, name, role, _variable in COMPTES_DEMO:
            user, _created = Utilisateur.objects.get_or_create(
                email=email,
                defaults={
                    "nom": name,
                    "role": role,
                    "actif": True,
                    "is_staff": False,
                },
            )
            user.nom = name
            user.role = role
            user.actif = True
            user.is_staff = False
            user.is_superuser = False
            user.set_password(passwords[email])
            user.save(
                update_fields=(
                    "nom",
                    "role",
                    "actif",
                    "is_staff",
                    "is_superuser",
                    "password",
                    "mot_de_passe_hash",
                )
            )
            PreferencesUtilisateur.objects.get_or_create(utilisateur=user)

        viewer = Utilisateur.objects.get(email=COMPTES_DEMO[3][0])
        for organisation in organisations_pilotes:
            UtilisateurOrganisation.objects.get_or_create(
                organisation=organisation,
                utilisateur=viewer,
            )

        self.stdout.write(
            self.style.SUCCESS(
                "Provisioned four demo accounts across three demo organizations."
            )
        )
