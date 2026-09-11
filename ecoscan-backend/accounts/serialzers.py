import re
from django.contrib.auth.tokens import default_token_generator
from django.core.mail import send_mail
from django.utils.encoding import force_bytes, force_str
from django.utils.http import urlsafe_base64_decode, urlsafe_base64_encode
from email_validator import EmailNotValidError, validate_email
from rest_framework import serializers
from rest_framework.validators import UniqueValidator

from .models import Utilisateur


class OnboardingAdminOrganisationSerializer(serializers.Serializer):
    """Sérialiseur pour la phase d'onboarding autonome d'un Admin d'organisation.
    
    Reçoit le nom de l'administrateur et l'e-mail unique sélectionné côté Front-End
    (qu'il s'agisse de l'e-mail générique d'organisation ou de son e-mail pro).
    Le compte est créé à l'état inactif, en attente de validation par le Super Admin.
    """
    nom_admin = serializers.CharField(max_length=150)
    email_connexion = serializers.EmailField()

    def validate_email_connexion(self, value):
        """Vérifie le domaine et normalise l'e-mail transmis par le Front-End."""
        try:
            email_info = validate_email(value, check_deliverability=True)
            email_valide = email_info.email
        except EmailNotValidError as error:
            raise serializers.ValidationError(f"E-mail invalide : {str(error)}")

        # Vérification de l'unicité de l'identifiant de connexion
        if Utilisateur.objects.filter(email=email_valide).exists():
            raise serializers.ValidationError("Cette adresse e-mail est déjà associée à un compte.")

        return email_valide

    def create(self, validated_data):
        """Crée le compte de l'Admin d'organisation à l'état inactif."""
        utilisateur = Utilisateur.objects.create(
            email=validated_data["email_connexion"],
            nom=validated_data["nom_admin"],
            role=Utilisateur.Role.ADMIN_ORGANISATION,
            actif=False,  # Bloqué tant que le Super Admin ne l'a pas validé
            is_staff=False
        )
        utilisateur.set_unusable_password()
        utilisateur.save()
        return utilisateur


class UtilisateurSerializer(serializers.ModelSerializer):
    """Sérialiseur standard pour la consultation et la mise à jour des profils utilisateurs.

    Ce sérialiseur applique les règles hiérarchiques sur la modification des rôles :
    - Un SUPER_ADMIN ne peut interagir qu'avec sa propre équipe (autres SUPER_ADMIN).
    - Un ADMIN_ORGANISATION actif gère les profils UTILISATEUR_ORGANISATION et CONSULTANT.
    """

    email = serializers.EmailField(
        validators=[
            UniqueValidator(
                queryset=Utilisateur.objects.all(),
                message="Cette adresse e-mail est déjà utilisée.",
            )
        ]
    )

    class Meta:
        model = Utilisateur
        fields = ("id", "nom", "email", "role", "date_creation", "actif")
        read_only_fields = ("id", "date_creation")

    def validate_email(self, value):
        """Vérifie et normalise l'e-mail à l'aide de la bibliothèque email-validator."""
        try:
            email_info = validate_email(value, check_deliverability=True)
            return email_info.email
        except EmailNotValidError as error:
            raise serializers.ValidationError(str(error))

    def validate(self, data):
        """Validation globale des autorisations de modification selon l'acteur connecté."""
        request = self.context.get("request")
        if not request or not request.user:
            raise serializers.ValidationError("Authentification requise.")

        acteur = request.user
        nouveau_role = data.get("role")

        if self.instance and nouveau_role and self.instance.role != nouveau_role:
            
            # Règle SUPER_ADMIN : Équipe de dev uniquement
            if acteur.role == Utilisateur.Role.SUPER_ADMIN:
                if self.instance.role != Utilisateur.Role.SUPER_ADMIN or nouveau_role != Utilisateur.Role.SUPER_ADMIN:
                    raise serializers.ValidationError(
                        "En tant que Super Administrateur, vous ne pouvez modifier que les rôles de votre équipe de développement."
                    )

            # Règle ADMIN_ORGANISATION : Doit être actif et gère uniquement les accès de sa structure
            elif acteur.role == Utilisateur.Role.ADMIN_ORGANISATION:
                if not acteur.actif:
                    raise serializers.ValidationError("Votre compte Administrateur d'organisation doit être actif pour modifier des accès.")
                
                roles_geres = [Utilisateur.Role.UTILISATEUR_ORGANISATION, Utilisateur.Role.CONSULTANT]
                if nouveau_role not in roles_geres or self.instance.role not in roles_geres:
                    raise serializers.ValidationError(
                        "En tant qu'Admin d'organisation, vous ne pouvez gérer que les accès des Utilisateurs et Consultants."
                    )
            else:
                raise serializers.ValidationError("Vous n'avez pas l'autorisation de modifier ce rôle.")

        return data


class InvitationCreateSerializer(serializers.ModelSerializer):
    """Sérialiseur dédié à la création par invitation de nouveaux utilisateurs.

    Il crée le compte à l'état inactif (sans mot de passe initial) et expédie un e-mail 
    contenant un jeton (token) de sécurité pour permettre à l'utilisateur de s'activer.
    """

    email = serializers.EmailField(
        validators=[
            UniqueValidator(
                queryset=Utilisateur.objects.all(),
                message="Cette adresse e-mail est déjà utilisée.",
            )
        ]
    )

    class Meta:
        model = Utilisateur
        fields = ("id", "nom", "email", "role")
        read_only_fields = ("id",)

    def validate_email(self, value):
        """Vérifie le domaine et normalise l'e-mail grâce à email-validator."""
        try:
            email_info = validate_email(value, check_deliverability=True)
            return email_info.email
        except EmailNotValidError as error:
            raise serializers.ValidationError(str(error))

    def validate(self, data):
        """Valide les droits de création par invitation selon le rôle de l'utilisateur connecté."""
        request = self.context.get("request")
        if not request or not request.user:
            raise serializers.ValidationError("Authentification requise.")

        acteur = request.user
        role_cible = data.get("role")

        if acteur.role == Utilisateur.Role.ADMIN_ORGANISATION:
            if not acteur.actif:
                raise serializers.ValidationError("Votre compte Administrateur d'organisation doit être actif pour inviter des membres.")
            
            roles_autorises = [Utilisateur.Role.UTILISATEUR_ORGANISATION, Utilisateur.Role.CONSULTANT]
            if role_cible not in roles_autorises:
                raise serializers.ValidationError("Vous ne pouvez créer que des profils Utilisateur organisation ou Consultant.")
        
        elif acteur.role == Utilisateur.Role.SUPER_ADMIN:
            if role_cible != Utilisateur.Role.SUPER_ADMIN:
                raise serializers.ValidationError("Vous ne pouvez créer que des membres de type Super Administrateur.")
        else:
            raise serializers.ValidationError("Vous n'avez pas l'autorisation de créer un compte.")

        return data

    def create(self, validated_data):
        """Crée l'utilisateur de manière inactive et déclenche l'envoi du mail d'invitation."""
        utilisateur = Utilisateur.objects.create(
            actif=False,
            is_staff=False,
            **validated_data
        )
        utilisateur.set_unusable_password()
        utilisateur.save()

        uid = urlsafe_base64_encode(force_bytes(utilisateur.pk))
        token = default_token_generator.make_token(utilisateur)
        lien_activation = f"https://monapp.com?uid={uid}&token={token}"

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
    """Sérialiseur de traitement du lien d'invitation/activation envoyé par e-mail.
    
    Reçoit le token, l'uid et applique le nouveau mot de passe (UX souple : 8 car, 1 lettre, 1 chiffre)
    pour activer définitivement le profil de l'Admin d'organisation, de l'Utilisateur ou du Consultant.
    """
    uid = serializers.CharField()
    token = serializers.CharField()
    mot_de_passe = serializers.CharField(write_only=True)

    def validate_mot_de_passe(self, value):
        """Valide uniquement la structure requise du mot de passe."""
        if len(value) < 8:
            raise serializers.ValidationError("Le mot de passe doit contenir au moins 8 caractères.")

        if not re.match(r"^(?=.*[A-Za-z])(?=.*\d).+$", value):
            raise serializers.ValidationError(
                "Le mot de passe doit être composé d'au moins une lettre et un chiffre."
            )
        return value

    def validate(self, data):
        """Valide la concordance et l'expiration du jeton d'invitation."""
        try:
            uid_decode = force_str(urlsafe_base64_decode(data["uid"]))
            utilisateur = Utilisateur.objects.get(pk=uid_decode)
        except (TypeError, ValueError, OverflowError, Utilisateur.DoesNotExist):
            raise serializers.ValidationError("Le lien d'invitation est invalide ou corrompu.")

        if not default_token_generator.check_token(utilisateur, data["token"]):
            raise serializers.ValidationError("Ce lien d'invitation a expiré ou a déjà été utilisé.")

        self.context["utilisateur"] = utilisateur
        return data

    def save(self):
        """Met à jour le mot de passe de l'utilisateur, active le compte et l'enregistre."""
        utilisateur = self.context["utilisateur"]
        mot_de_passe = self.validated_data["mot_de_passe"]
        utilisateur.set_password(mot_de_passe)
        utilisateur.actif = True
        utilisateur.save()
        return utilisateur