import re
from django.contrib.auth.tokens import default_token_generator
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.mail import send_mail
from django.utils.encoding import force_bytes, force_str
from django.utils.http import urlsafe_base64_encode, urlsafe_base64_decode
from email_validator import validate_email, EmailNotValidError
from rest_framework import serializers
from rest_framework.validators import UniqueValidator
from .models import Utilisateur


class OnboardingAdminOrganisationSerializer(serializers.Serializer):
    """Sérialiseur pour la phase d'onboarding autonome d'un Admin d'organisation.
    
    Reçoit le nom de l'administrateur et l'e-mail unique sélectionné côté Front-End.
    """
    nom_admin = serializers.CharField(
        max_length=150,
        error_messages={"blank": "Veuillez renseigner le nom du responsable."}
    )
    email_connexion = serializers.EmailField(
        error_messages={"blank": "L'adresse e-mail de connexion est obligatoire."}
    )

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

    def create(self, validated_data):
        """Crée le compte de l'Admin d'organisation à l'état inactif."""
        utilisateur = Utilisateur.objects.create(
            email=validated_data["email_connexion"],
            nom=validated_data["nom_admin"],
            role=Utilisateur.Role.ADMIN_ORGANISATION,
            actif=False,
            is_staff=False
        )
        utilisateur.set_unusable_password()
        utilisateur.save()
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

    class Meta:
        model = Utilisateur
        fields = ("id", "nom", "email", "role", "date_creation", "actif")
        read_only_fields = ("id", "date_creation")

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
        """Vérifie la validité réelle de l'e-mail invité."""
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
        
        elif acteur.role == Utilisateur.Role.SUPER_ADMIN:
            if role_cible != Utilisateur.Role.SUPER_ADMIN:
                raise serializers.ValidationError("Vous pouvez uniquement inviter des membres Super Administrateurs.")
        else:
            raise serializers.ValidationError("Vous n'avez pas l'autorisation d'inviter des membres sur la plateforme.")

        return data

    def create(self, validated_data):
        """Crée le compte inactif et envoie l'e-mail d'invitation."""
        utilisateur = Utilisateur.objects.create(
            actif=False,
            is_staff=False,
            **validated_data
        )
        utilisateur.set_unusable_password()
        utilisateur.save()

        uid = urlsafe_base64_encode(force_bytes(utilisateur.pk))
        token = default_token_generator.make_token(utilisateur)
        lien_activation = f"https://monapp.com/activation/?uid={uid}&token={token}"

        send_mail(
            subject="Invitation à rejoindre la plateforme",
            message=f"Bonjour {utilisateur.nom},\n\nVous avez été invité sur la plateforme.\n"
                    f"Veuillez finaliser la configuration de votre compte en définissant votre mot de passe "
                    f"via ce lien unique : {lien_activation}\n\nL'équipe.",
            from_email="noreply@monapp.com",
            recipient_list=[utilisateur.email],
            fail_silently=False,
        )
        return utilisateur


class FinaliserInscriptionSerializer(serializers.Serializer):
    """Sérialiseur gérant la création du mot de passe final lors de l'activation.
    
    Traduit et simplifie les erreurs des validateurs complexes de Django pour le Front-End.
    """
    uid = serializers.CharField(error_messages={"blank": "Identifiant utilisateur manquant."})
    token = serializers.CharField(error_messages={"blank": "Jeton de sécurité manquant."})
    mot_de_passe = serializers.CharField(write_only=True, error_messages={"blank": "Le mot de passe est obligatoire."})

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
        try:
            uid_decode = force_str(urlsafe_base64_decode(data["uid"]))
            utilisateur = Utilisateur.objects.get(pk=uid_decode)
        except (TypeError, ValueError, OverflowError, Utilisateur.DoesNotExist):
            raise serializers.ValidationError("Ce lien d'activation n'est pas ou plus valide.")
        
        if not default_token_generator.check_token(utilisateur, data["token"]):
            raise serializers.ValidationError("Ce lien d'invitation a expiré ou a déjà été utilisé pour configurer ce compte.")
        
        self.context["utilisateur"] = utilisateur
        return data

    def save(self):
        """Active le profil utilisateur avec le nouveau mot de passe."""
        utilisateur = self.context["utilisateur"]
        mot_de_passe = self.validated_data["mot_de_passe"]
        utilisateur.set_password(mot_de_passe)
        utilisateur.actif = True
        utilisateur.save()
        return utilisateur