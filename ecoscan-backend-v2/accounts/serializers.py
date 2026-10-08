import re
from django.contrib.auth.tokens import default_token_generator
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.utils.encoding import force_bytes, force_str
from django.utils.http import urlsafe_base64_encode, urlsafe_base64_decode
from email_validator import validate_email, EmailNotValidError
from rest_framework import serializers
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer
from rest_framework.validators import UniqueValidator
import logging
from organizations.models import Organisation, UtilisateurOrganisation
from billing.models import Plan
from .models import Utilisateur, PreferencesUtilisateur
from django.db import transaction
from .services import envoyer_email_activation

logger = logging.getLogger(__name__)

class OnboardingAdminOrganisationSerializer(serializers.Serializer):
    """Sérialiseur pour la phase d'onboarding autonome d'un Admin d'organisation.
    
    Crée une demande organisationnelle en attente et son administrateur inactif.
    """
    nom_admin = serializers.CharField(
        max_length=150,
        error_messages={"blank": "Veuillez renseigner le nom du responsable."}
    )
    nom_organisation = serializers.CharField(
        max_length=180,
        error_messages={"blank": "Veuillez renseigner le nom de votre organisation."}
    )
    email_connexion = serializers.EmailField(
        error_messages={"blank": "L'adresse e-mail de connexion est obligatoire."}
    )
    secteur = serializers.CharField(max_length=120)
    localisation = serializers.CharField(max_length=255)
    details_demande = serializers.JSONField(required=False, default=dict)

    def validate_email_connexion(self, value):
        """Vérifie le domaine, normalise l'e-mail et contrôle l'unicité avec des messages UX clairs."""
        try:
            email_info = validate_email(value, check_deliverability=True)
            email_valide = email_info.email
        except EmailNotValidError:
            raise serializers.ValidationError(
                "Cette adresse e-mail semble incorrecte. Vérifiez le format (ex: nom@entreprise.com)."
            )

        if Utilisateur.objects.filter(email=email_valide).exists():
            raise serializers.ValidationError(
                "Cette adresse e-mail est déjà associée à un compte. Veuillez en utiliser une autre ou vous connecter."
            )

        return email_valide

    def validate_nom_organisation(self, value):
        value = value.strip()
        if Organisation.objects.filter(nom__iexact=value).exists():
            raise serializers.ValidationError(
                "Une demande ou une organisation portant déjà ce nom existe."
            )
        return value

    def validate_details_demande(self, value):
        if not isinstance(value, dict):
            raise serializers.ValidationError("Les informations complémentaires doivent être un objet.")
        allowed_fields = {"profil", "nombre_sites", "sources", "maturite", "objectifs"}
        details = {key: value[key] for key in allowed_fields if key in value}
        plan_id = value.get("plan_id")
        if not plan_id:
            raise serializers.ValidationError("Choisissez une formule avant de créer votre espace.")
        try:
            plan = Plan.objects.get(pk=plan_id, actif=True)
        except (Plan.DoesNotExist, DjangoValidationError, ValueError, TypeError):
            raise serializers.ValidationError("La formule sélectionnée n’est plus disponible.")
        details["plan_id"] = str(plan.pk)
        details["plan_nom"] = plan.nom
        return details

    def create(self, validated_data):
        """Crée atomiquement le compte, l'organisation en attente et leur lien."""
        with transaction.atomic():
            utilisateur = Utilisateur.objects.create(
                email=validated_data["email_connexion"],
                nom=validated_data["nom_admin"],
                role=Utilisateur.Role.ADMIN_ORGANISATION,
                actif=False,
                is_staff=False,
            )
            utilisateur.set_unusable_password()
            utilisateur.save(update_fields=("password", "mot_de_passe_hash"))

            details_demande = validated_data.get("details_demande", {})
            details_demande["inscription_autonome"] = True
            organisation = Organisation.objects.create(
                nom=validated_data["nom_organisation"],
                secteur=validated_data["secteur"],
                localisation=validated_data["localisation"],
                statut=Organisation.Statut.EN_ATTENTE,
                details_demande=validated_data.get("details_demande", {}),
            )
            UtilisateurOrganisation.objects.create(
                organisation=organisation,
                utilisateur=utilisateur,
            )

        return utilisateur


class UtilisateurSerializer(serializers.ModelSerializer):
    """Sérialiseur standard pour la consultation et la mise à jour des profils utilisateurs.
    
    Applique le cloisonnement hiérarchique avec des retours d'erreurs explicites.
    """
    email = serializers.EmailField(
        validators=[
            UniqueValidator(
                queryset=Utilisateur.objects.all(),
                message="Cette adresse e-mail est déjà utilisée par un autre membre.",
            )
        ],
        error_messages={"blank": "L'adresse e-mail est obligatoire."}
    )
    nom = serializers.CharField(
        error_messages={"blank": "Le nom est obligatoire."}
    )
    invitation_en_attente = serializers.SerializerMethodField()

    class Meta:
        model = Utilisateur
        fields = ("id", "nom", "email", "role", "date_creation", "actif", "invitation_en_attente")
        read_only_fields = ("id", "date_creation", "actif")

    def get_invitation_en_attente(self, utilisateur):
        return not utilisateur.has_usable_password()

    def validate_email(self, value):
        """Vérifie et normalise l'e-mail avec email-validator."""
        try:
            email_info = validate_email(value, check_deliverability=True)
            return email_info.email
        except EmailNotValidError:
            raise serializers.ValidationError("Le format de l'adresse e-mail n'est pas valide.")

    def validate(self, data):
        """Validation des droits de modification de rôle avec messages d'erreurs métiers transparents."""
        request = self.context.get("request")
        if not request or not request.user:
            raise serializers.ValidationError("Vous devez être connecté pour effectuer cette action.")

        acteur = request.user
        nouveau_role = data.get("role")

        if self.instance and nouveau_role and self.instance.role != nouveau_role:
            
            if acteur.role == Utilisateur.Role.SUPER_ADMIN:
                if self.instance.role != Utilisateur.Role.SUPER_ADMIN or nouveau_role != Utilisateur.Role.SUPER_ADMIN:
                    raise serializers.ValidationError(
                        "En tant que Super Administrateur, vous pouvez uniquement modifier les rôles de votre équipe de développement."
                    )

            elif acteur.role == Utilisateur.Role.ADMIN_ORGANISATION:
                if not acteur.actif:
                    raise serializers.ValidationError(
                        "Votre compte doit être validé et actif pour pouvoir modifier des accès."
                    )
                
                roles_geres = [Utilisateur.Role.UTILISATEUR_ORGANISATION, Utilisateur.Role.CONSULTANT]
                if nouveau_role not in roles_geres or self.instance.role not in roles_geres:
                    raise serializers.ValidationError(
                        "En tant qu'Administrateur, vous pouvez uniquement attribuer les rôles d'Utilisateur ou de Consultant."
                    )
            else:
                raise serializers.ValidationError("Vous n'avez pas les permissions nécessaires pour modifier ce rôle.")

        return data


class InvitationCreateSerializer(serializers.ModelSerializer):
    """Sérialiseur pour inviter des membres, avec gestion des erreurs d'attribution."""
    email = serializers.EmailField(
        validators=[
            UniqueValidator(
                queryset=Utilisateur.objects.all(),
                message="Ce collaborateur est déjà inscrit sur la plateforme.",
            )
        ],
        error_messages={"blank": "L'adresse e-mail de votre collaborateur est obligatoire."}
    )
    nom = serializers.CharField(error_messages={"blank": "Le nom du collaborateur est obligatoire."})
    role = serializers.CharField(error_messages={"blank": "Veuillez attribuer un rôle à ce membre."})

    class Meta:
        model = Utilisateur
        fields = ("id", "nom", "email", "role")
        read_only_fields = ("id",)

    def validate_email(self, value):
        try:
            email_info = validate_email(value, check_deliverability=True)
            return email_info.email
        except EmailNotValidError:
            raise serializers.ValidationError("L'adresse e-mail saisie n'existe pas ou son format est incorrect.")

    def validate(self, data):
        """Contrôle les droits d'invitation."""
        request = self.context.get("request")
        if not request or not request.user:
            raise serializers.ValidationError("Une session active est requise pour envoyer une invitation.")

        acteur = request.user
        role_cible = data.get("role")

        if acteur.role == Utilisateur.Role.ADMIN_ORGANISATION:
            if not acteur.actif:
                raise serializers.ValidationError("Votre compte d'administrateur doit être actif pour inviter des membres.")

            roles_autorises = [Utilisateur.Role.UTILISATEUR_ORGANISATION, Utilisateur.Role.CONSULTANT]
            if role_cible not in roles_autorises:
                raise serializers.ValidationError("Vous pouvez uniquement inviter des Utilisateurs ou des Consultants.")

            organisation = Organisation.objects.filter(membres__utilisateur=acteur).first()
            if organisation is None:
                raise serializers.ValidationError(
                    "Vous devez d'abord configurer votre propre organisation avant d'inviter des membres."
                )
            self._organisation_cible = organisation

        elif acteur.role == Utilisateur.Role.SUPER_ADMIN:
            if role_cible != Utilisateur.Role.SUPER_ADMIN:
                raise serializers.ValidationError("Vous pouvez uniquement inviter des membres Super Administrateurs.")
            self._organisation_cible = None
        else:
            raise serializers.ValidationError("Vous n'avez pas l'autorisation d'inviter des membres sur la plateforme.")

        return data

    def create(self, validated_data):
        """Crée le compte inactif et le rattache à l'organisation de l'inviteur.
        L'envoi d'email est volontairement séparé de la transaction et ne doit
        JAMAIS faire échouer la création du compte — même principe de dégradation
        gracieuse que ai_client.py (une panne de service tiers ne bloque pas
        l'action métier)."""

        with transaction.atomic():
            utilisateur = Utilisateur.objects.create(
                actif=False,
                is_staff=False,
                **validated_data
            )
            utilisateur.set_unusable_password()
            utilisateur.save()

            if self._organisation_cible is not None:
                UtilisateurOrganisation.objects.create(
                    organisation=self._organisation_cible,
                    utilisateur=utilisateur,
                )

        self.context["activation_email_sent"] = False
        try:
            envoyer_email_activation(
                utilisateur,
                sujet="Invitation à rejoindre la plateforme",
                introduction="Vous avez été invité à rejoindre la plateforme EcoScan.",
            )
            self.context["activation_email_sent"] = True
        except Exception:
            logger.exception("L'invitation de %s est créée, mais l'e-mail n'a pas été envoyé.", utilisateur.email)
            # Le compte et le rattachement à l'organisation restent valides même
            # si l'email échoue — l'admin peut relancer l'envoi manuellement
            # (via un futur endpoint "renvoyer l'invitation" si le besoin se confirme).

        return utilisateur


class FinaliserInscriptionSerializer(serializers.Serializer):
    """Sérialiseur gérant la création du mot de passe final lors de l'activation.
    
    Traduit et simplifie les erreurs des validateurs complexes de Django pour le Front-End.
    """
    uid = serializers.CharField(error_messages={"blank": "Identifiant utilisateur manquant."})
    token = serializers.CharField(error_messages={"blank": "Jeton de sécurité manquant."})
    mot_de_passe = serializers.CharField(write_only=True, error_messages={"blank": "Le mot de passe est obligatoire."})
    mot_de_passe_confirmation = serializers.CharField(
        write_only=True,
        error_messages={"blank": "La confirmation du mot de passe est obligatoire."},
    )

    def validate_mot_de_passe(self, value):
        """Filtre le mot de passe selon l'UX et convertit les erreurs Django brutes en messages fluides."""
        # 1. Contraintes graphiques minimales
        if len(value) < 8:
            raise serializers.ValidationError("Le mot de passe doit contenir au moins 8 caractères.")
        if not re.match(r"^(?=.*[A-Za-z])(?=.*\d).+$", value):
            raise serializers.ValidationError("Le mot de passe doit mélanger au moins une lettre et un chiffre.")

        # 2. Politique stricte de Django (settings.py) avec traduction UX humaine
        utilisateur_concerne = self.context.get("utilisateur")
        try:
            validate_password(value, user=utilisateur_concerne)
        except DjangoValidationError as error:
            erreurs_ux = []
            for msg in error.messages:
                if "too common" in msg.lower() or "commun" in msg.lower():
                    erreurs_ux.append("Ce mot de passe est trop simple et facile à deviner. Choisissez-en un plus original.")
                elif "entirely numeric" in msg.lower() or "numérique" in msg.lower():
                    erreurs_ux.append("Le mot de passe ne peut pas contenir uniquement des chiffres.")
                elif "attributes" in msg.lower() or "similaire" in msg.lower():
                    erreurs_ux.append("Le mot de passe ressemble trop à vos informations personnelles (nom ou e-mail).")
                else:
                    erreurs_ux.append(msg)
            raise serializers.ValidationError(erreurs_ux)
        return value

    def validate(self, data):
        """Valide la validité temporelle et technique du lien d'invitation."""
        if data["mot_de_passe"] != data["mot_de_passe_confirmation"]:
            raise serializers.ValidationError({
                "mot_de_passe_confirmation": "Les mots de passe ne correspondent pas."
            })

        try:
            uid_decode = force_str(urlsafe_base64_decode(data["uid"]))
            utilisateur = Utilisateur.objects.get(pk=uid_decode)
        except (TypeError, ValueError, OverflowError, Utilisateur.DoesNotExist):
            raise serializers.ValidationError("Ce lien d'activation n'est pas ou plus valide.")
        
        if not default_token_generator.check_token(utilisateur, data["token"]):
            raise serializers.ValidationError("Ce lien d'invitation a expiré ou a déjà été utilisé pour configurer ce compte.")

        organisations_en_essai = list(
            Organisation.objects.filter(
                membres__utilisateur=utilisateur,
                statut=Organisation.Statut.EN_ATTENTE,
                details_demande__inscription_autonome=True,
            )
        )
        for organisation in organisations_en_essai:
            plan_id = organisation.details_demande.get("plan_id")
            plan_disponible = (
                Plan.objects.filter(pk=plan_id, actif=True).exists()
                if plan_id
                else Plan.objects.filter(code="standard", actif=True).exists()
            )
            if not plan_disponible:
                raise serializers.ValidationError(
                    "La formule choisie n’est plus disponible. Contactez EcoScan pour finaliser votre inscription."
                )

        self.context["utilisateur"] = utilisateur
        self.context["organisations_en_essai"] = organisations_en_essai
        return data

    def save(self):
        """Active le compte et démarre l'essai après vérification de l'e-mail."""
        utilisateur = self.context["utilisateur"]
        mot_de_passe = self.validated_data["mot_de_passe"]
        with transaction.atomic():
            utilisateur.set_password(mot_de_passe)
            utilisateur.actif = True
            utilisateur.save(update_fields=("password", "mot_de_passe_hash", "actif"))
            for organisation in self.context["organisations_en_essai"]:
                organisation.statut = Organisation.Statut.ACTIVE
                organisation.save(update_fields=("statut",))
        self.context["essai_demarre"] = bool(self.context["organisations_en_essai"])
        return utilisateur


class ConnexionSerializer(TokenObtainPairSerializer):
    """Ajoute les informations nécessaires au front (rôle, nom) directement dans le JWT."""

    @classmethod
    def get_token(cls, user):
        token = super().get_token(user)
        token['role'] = user.role
        token['nom'] = user.nom
        token['email'] = user.email
        return token


class PreferencesUtilisateurSerializer(serializers.ModelSerializer):
    class Meta:
        model = PreferencesUtilisateur
        fields = (
            "theme", "densite", "accent",
            "alertes_email", "briefing_quotidien", "detection_anomalies", "rapport_hebdomadaire",
            "delai_inactivite_minutes",
        )

class ChangerMotDePasseSerializer(serializers.Serializer):
    """Exige l'ancien mot de passe pour toute modification — jamais de
    changement silencieux sans preuve de connaissance du mot de passe actuel."""
    ancien_mot_de_passe = serializers.CharField(write_only=True)
    nouveau_mot_de_passe = serializers.CharField(write_only=True)

    def validate_ancien_mot_de_passe(self, value):
        utilisateur = self.context["request"].user
        if not utilisateur.check_password(value):
            raise serializers.ValidationError("Le mot de passe actuel est incorrect.")
        return value

    def validate_nouveau_mot_de_passe(self, value):
        # Réutilise les mêmes règles UX déjà écrites pour FinaliserInscriptionSerializer,
        # plutôt que d'en réinventer une version divergente ici.
        if len(value) < 8:
            raise serializers.ValidationError("Le mot de passe doit contenir au moins 8 caractères.")
        if not re.match(r"^(?=.*[A-Za-z])(?=.*\d).+$", value):
            raise serializers.ValidationError("Le mot de passe doit mélanger au moins une lettre et un chiffre.")

        utilisateur = self.context["request"].user
        try:
            validate_password(value, user=utilisateur)
        except DjangoValidationError as error:
            raise serializers.ValidationError(list(error.messages))
        return value

    def save(self):
        utilisateur = self.context["request"].user
        utilisateur.set_password(self.validated_data["nouveau_mot_de_passe"])
        utilisateur.save()
        return utilisateur
