import hashlib

def calculer_hash_fichier(fichier) -> str:
    contenu = fichier.read()
    fichier.seek(0)
    return hashlib.sha256(contenu).hexdigest()