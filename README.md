# Myfxbook Telegram Agent — zéro PC allumé

Agent Python qui lit le calendrier économique Myfxbook et envoie chaque jour les news importantes sur Telegram via GitHub Actions.

## Solution recommandée

- **Hébergement** : GitHub Actions
- **Coût** : 0 DH si repo public. En repo privé, l'exécution quotidienne consomme très peu de minutes, mais dépend du quota GitHub de ton compte.
- **PC allumé** : non
- **Telegram** : bot gratuit via BotFather

## Installation rapide

### 1) Créer le bot Telegram

1. Ouvre Telegram.
2. Cherche `@BotFather`.
3. Envoie `/newbot`.
4. Donne un nom, puis un username qui finit par `bot`.
5. Copie le token, exemple : `123456789:ABC...`

### 2) Récupérer ton chat_id

1. Envoie un message à ton nouveau bot, par exemple `test`.
2. Dans ton navigateur, ouvre :

```text
https://api.telegram.org/botTON_TOKEN/getUpdates
```

3. Remplace `TON_TOKEN` par le token du bot.
4. Cherche `"chat":{"id":...}`.
5. Copie ce nombre : c’est ton `TELEGRAM_CHAT_ID`.

### 3) Créer le repo GitHub

1. Crée un nouveau repo, par exemple `myfxbook-telegram-agent`.
2. Mets ces fichiers dans le repo :
   - `agent.py`
   - `requirements.txt`
   - `.github/workflows/calendar.yml`

### 4) Ajouter les secrets GitHub

Va dans :

```text
GitHub repo > Settings > Secrets and variables > Actions > New repository secret
```

Ajoute :

```text
TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID
```

### 5) Tester

Va dans :

```text
Actions > Myfxbook Telegram Calendar > Run workflow
```

Tu dois recevoir le message Telegram.

## Réglages utiles

Dans `.github/workflows/calendar.yml` :

```yaml
CURRENCIES: "USD"
IMPACTS: "High"
LOOKAHEAD_DAYS: "7"
```

Pour XAUUSD / US100 scalping, garde surtout `USD` + `High`.

Pour ajouter EUR et GBP :

```yaml
CURRENCIES: "USD,EUR,GBP"
```

Pour recevoir High + Medium :

```yaml
IMPACTS: "High,Medium"
```

## Heure d'envoi

Par défaut :

```yaml
- cron: "17 8 * * *"
  timezone: "Africa/Casablanca"
```

Donc l’agent tourne chaque jour à **08:17 heure Maroc**.

## Important sur les heures Myfxbook

Le site Myfxbook non connecté peut afficher les horaires en UTC/GMT. L’agent utilise :

```yaml
SOURCE_TIMEZONE: "UTC"
TIMEZONE: "Africa/Casablanca"
```

Donc une news à 12:30 UTC sera envoyée à 13:30 Maroc si le Maroc est en UTC+1.

Si ton Myfxbook connecté affiche déjà l’heure Maroc, remplace :

```yaml
SOURCE_TIMEZONE: "Africa/Casablanca"
```

## Limites honnêtes

- GitHub Actions peut retarder une exécution de quelques minutes.
- Myfxbook peut changer son HTML, ce qui peut casser le parsing.
- Pour du trading réel, utilise le message comme filtre de risque, pas comme signal d’entrée.
