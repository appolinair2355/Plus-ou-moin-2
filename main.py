import os
import asyncio
import re
import json
import zipfile
import tempfile
import shutil
import glob
from datetime import datetime, timedelta
from telethon import TelegramClient, events
from telethon.events import ChatAction
from dotenv import load_dotenv
from predictor import CardPredictor
from yaml_manager import init_database, db
from excel_importer import ExcelPredictionManager
from aiohttp import web
import threading

# Load environment variables
load_dotenv()

# --- CONFIGURATION ---
try:
    API_ID = int(os.getenv('API_ID') or '0')
    API_HASH = os.getenv('API_HASH') or ''
    BOT_TOKEN = os.getenv('BOT_TOKEN') or ''
    ADMIN_ID = int(os.getenv('ADMIN_ID') or '0') if os.getenv('ADMIN_ID') else None
    PORT = int(os.getenv('PORT') or '5000')
    DISPLAY_CHANNEL = int(os.getenv('DISPLAY_CHANNEL') or '-1002999811353')

    # Validation des variables requises
    if not API_ID or API_ID == 0:
        raise ValueError("API_ID manquant ou invalide")
    if not API_HASH:
        raise ValueError("API_HASH manquant")
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN manquant")

    print(f"✅ Configuration chargée: API_ID={API_ID}, ADMIN_ID={ADMIN_ID or 'Non configuré'}, PORT={PORT}, DISPLAY_CHANNEL={DISPLAY_CHANNEL}")
except Exception as e:
    print(f"❌ Erreur configuration: {e}")
    print("Vérifiez vos variables d'environnement")
    exit(1)

# Fichier de configuration persistante
CONFIG_FILE = 'bot_config.json'

# Variables d'état
detected_stat_channel = None
detected_display_channel = None
confirmation_pending = {}
prediction_interval = 5  # Intervalle en minutes

# Variable pour le décalage de prédiction (N+a)
a_offset = 1  # Valeur par défaut, modifiable avec /a

# Variable pour l'offset de vérification (r)
# Définit le nombre d'essais pour vérifier une prédiction (2-10)
r_offset = 2  # Valeur par défaut, modifiable avec /r

# Emojis de vérification selon l'offset (N+0, N+1, N+2, etc.)
# L'index correspond au nombre d'essais: 0 = 1er essai, 1 = 2ème essai, etc.
VERIFICATION_EMOJIS = {
    0: "✅0️⃣",  # 1er essai (N+0)
    1: "✅1️⃣",  # 2ème essai (N+1)
    2: "✅2️⃣",  # 3ème essai (N+2)
    3: "✅3️⃣",  # 4ème essai (N+3)
    4: "✅4️⃣",  # 5ème essai (N+4)
    5: "✅5️⃣",  # 6ème essai (N+5)
    6: "✅6️⃣",  # 7ème essai (N+6)
    7: "✅7️⃣",  # 8ème essai (N+7)
    8: "✅8️⃣",  # 9ème essai (N+8)
    9: "✅9️⃣",  # 10ème essai (N+9)
    10: "✅🔟"  # 11ème essai (N+10)
}

# Dictionnaire pour stocker les prédictions actives et leur statut
active_predictions = {}  # {numero_predit: {"message_id": id, "channel_id": id, "expected": "joueur/banquier", "attempts": 0}}

# Variables pour la détection automatique des fichiers Excel
EXCEL_WATCH_DIR = "."  # Répertoire à surveiller
processed_excel_files = set()  # Fichiers déjà traités
last_excel_check = None  # Dernière vérification

def load_config():
    """Load configuration with priority: JSON > Database > Environment"""
    global detected_stat_channel, detected_display_channel, prediction_interval, a_offset, r_offset, active_predictions
    try:
        # Toujours essayer JSON en premier (source de vérité)
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                config = json.load(f)
                detected_stat_channel = config.get('stat_channel')
                detected_display_channel = config.get('display_channel', DISPLAY_CHANNEL)
                prediction_interval = config.get('prediction_interval', 1)
                a_offset = config.get('a_offset', 1)
                r_offset = config.get('r_offset', 2)
                active_predictions = config.get('active_predictions', {})
                print(f"✅ Configuration chargée depuis JSON: Stats={detected_stat_channel}, Display={detected_display_channel}, a_offset={a_offset}, r_offset={r_offset}")
                return

        # Fallback sur base de données si JSON n'existe pas
        if db:
            detected_stat_channel = db.get_config('stat_channel')
            detected_display_channel = db.get_config('display_channel') or DISPLAY_CHANNEL
            interval_config = db.get_config('prediction_interval')
            if detected_stat_channel:
                detected_stat_channel = int(detected_stat_channel)
            if detected_display_channel:
                detected_display_channel = int(detected_display_channel)
            if interval_config:
                prediction_interval = int(interval_config)
            print(f"✅ Configuration chargée depuis la DB: Stats={detected_stat_channel}, Display={detected_display_channel}, Intervalle={prediction_interval}min")
        else:
            # Utiliser le canal de display par défaut depuis les variables d'environnement
            detected_display_channel = DISPLAY_CHANNEL
            prediction_interval = 1
            print(f"ℹ️ Configuration par défaut: Display={detected_display_channel}, Intervalle={prediction_interval}min")
    except Exception as e:
        print(f"⚠️ Erreur chargement configuration: {e}")
        # Valeurs par défaut en cas d'erreur
        detected_stat_channel = None
        detected_display_channel = DISPLAY_CHANNEL
        prediction_interval = 1

def save_config():
    """Save configuration to database and JSON backup"""
    try:
        if db:
            # Sauvegarde en base de données
            db.set_config('stat_channel', detected_stat_channel)
            db.set_config('display_channel', detected_display_channel)
            db.set_config('prediction_interval', prediction_interval)
            db.set_config('a_offset', a_offset)
            print("💾 Configuration sauvegardée en base de données")

        # Sauvegarde JSON de secours
        config = {
            'stat_channel': detected_stat_channel,
            'display_channel': detected_display_channel,
            'prediction_interval': prediction_interval,
            'a_offset': a_offset,
            'r_offset': r_offset,
            'active_predictions': active_predictions
        }
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(config, f, indent=2)
        print(f"💾 Configuration sauvegardée: Stats={detected_stat_channel}, Display={detected_display_channel}, a_offset={a_offset}, r_offset={r_offset}")
    except Exception as e:
        print(f"❌ Erreur sauvegarde configuration: {e}")

def update_channel_config(source_id: int, target_id: int):
    """Update channel configuration"""
    global detected_stat_channel, detected_display_channel
    detected_stat_channel = source_id
    detected_display_channel = target_id
    save_config()

# Initialize database
database = init_database()

# Gestionnaire de prédictions
predictor = CardPredictor()

# Gestionnaire d'importation Excel
excel_manager = ExcelPredictionManager()

# Initialize Telegram client with unique session name
import time
session_name = f'bot_session_{int(time.time())}'
client = TelegramClient(session_name, API_ID, API_HASH)

async def start_bot():
    """Start the bot with proper error handling"""
    try:
        # Load saved configuration first
        load_config()

        await client.start(bot_token=BOT_TOKEN)
        print("Bot démarré avec succès...")

        # Get bot info
        me = await client.get_me()
        username = getattr(me, 'username', 'Unknown') or f"ID:{getattr(me, 'id', 'Unknown')}"
        print(f"Bot connecté: @{username}")

    except Exception as e:
        print(f"Erreur lors du démarrage du bot: {e}")
        return False

    return True

# --- INVITATION / CONFIRMATION ---
@client.on(events.ChatAction())
async def handler_join(event):
    """Handle bot joining channels/groups"""
    global confirmation_pending

    try:
        # Ignorer les événements d'épinglage de messages
        if event.new_pin or event.unpin:
            return

        # Ignorer les événements sans user_id (comme les épinglages)
        if not event.user_id:
            return

        print(f"ChatAction event: {event}")
        print(f"user_joined: {event.user_joined}, user_added: {event.user_added}")
        print(f"user_id: {event.user_id}, chat_id: {event.chat_id}")

        if event.user_joined or event.user_added:
            me = await client.get_me()
            me_id = getattr(me, 'id', None)
            print(f"Mon ID: {me_id}, Event user_id: {event.user_id}")

            if event.user_id == me_id:
                confirmation_pending[event.chat_id] = 'waiting_confirmation'

                # Get channel info
                try:
                    chat = await client.get_entity(event.chat_id)
                    chat_title = getattr(chat, 'title', f'Canal {event.chat_id}')
                except:
                    chat_title = f'Canal {event.chat_id}'

                # Send private invitation to admin
                invitation_msg = f"""🔔 **Nouveau canal détecté**

📋 **Canal** : {chat_title}
🆔 **ID** : {event.chat_id}

**Choisissez le type de canal** :
• `/set_stat {event.chat_id}` - Canal de statistiques
• `/set_display {event.chat_id}` - Canal de diffusion

Envoyez votre choix en réponse à ce message."""

                try:
                    await client.send_message(ADMIN_ID, invitation_msg)
                    print(f"Invitation envoyée à l'admin pour le canal: {chat_title} ({event.chat_id})")
                except Exception as e:
                    print(f"Erreur envoi invitation privée: {e}")
                    # Fallback: send to the channel temporarily for testing
                    await client.send_message(event.chat_id, f"⚠️ Impossible d'envoyer l'invitation privée. Canal ID: {event.chat_id}")
                    print(f"Message fallback envoyé dans le canal {event.chat_id}")
    except Exception as e:
        print(f"Erreur dans handler_join: {e}")

@client.on(events.NewMessage(pattern=r'/set_stat (-?\d+)'))
async def set_stat_channel(event):
    """Set statistics channel (only admin in private)"""
    global detected_stat_channel, confirmation_pending

    try:
        # Only allow in private chat with admin
        if event.is_group or event.is_channel:
            return

        if ADMIN_ID and event.sender_id != ADMIN_ID:
            await event.respond("❌ Seul l'administrateur peut configurer les canaux")
            return

        # Extract channel ID from command
        match = event.pattern_match
        channel_id = int(match.group(1))

        # Check if channel is waiting for confirmation
        if channel_id not in confirmation_pending:
            await event.respond("❌ Ce canal n'est pas en attente de configuration")
            return

        detected_stat_channel = channel_id
        confirmation_pending[channel_id] = 'configured_stat'

        # Save configuration
        save_config()

        try:
            chat = await client.get_entity(channel_id)
            chat_title = getattr(chat, 'title', f'Canal {channel_id}')
        except:
            chat_title = f'Canal {channel_id}'

        await event.respond(f"✅ **Canal de statistiques configuré**\n📋 {chat_title}\n\n✨ Le bot surveillera ce canal pour les prédictions - développé par Sossou Kouamé Appolinaire\n💾 Configuration sauvegardée automatiquement")
        print(f"Canal de statistiques configuré: {channel_id}")

    except Exception as e:
        print(f"Erreur dans set_stat_channel: {e}")

@client.on(events.NewMessage(pattern=r'/force_set_stat (-?\d+)'))
async def force_set_stat_channel(event):
    """Force set statistics channel without waiting for invitation (admin only)"""
    global detected_stat_channel

    try:
        # Only allow admin
        if ADMIN_ID and event.sender_id != ADMIN_ID:
            await event.respond("❌ Seul l'administrateur peut configurer les canaux")
            return

        # Extract channel ID from command
        match = event.pattern_match
        channel_id = int(match.group(1))

        detected_stat_channel = channel_id

        # Save configuration
        save_config()

        try:
            chat = await client.get_entity(channel_id)
            chat_title = getattr(chat, 'title', f'Canal {channel_id}')
        except:
            chat_title = f'Canal {channel_id}'

        await event.respond(f"✅ **Canal de statistiques configuré (force)**\n📋 {chat_title}\n🆔 ID: {channel_id}\n\n✨ Le bot surveillera ce canal pour les prédictions\n💾 Configuration sauvegardée automatiquement")
        print(f"Canal de statistiques configuré (force): {channel_id}")

    except Exception as e:
        print(f"Erreur dans force_set_stat_channel: {e}")
        await event.respond(f"❌ Erreur: {e}")

@client.on(events.NewMessage(pattern=r'/set_display (-?\d+)'))
async def set_display_channel(event):
    """Set display channel (only admin in private)"""
    global detected_display_channel, confirmation_pending

    try:
        # Only allow in private chat with admin
        if event.is_group or event.is_channel:
            return

        if event.sender_id != ADMIN_ID:
            await event.respond("❌ Seul l'administrateur peut configurer les canaux")
            return

        # Extract channel ID from command
        match = event.pattern_match
        channel_id = int(match.group(1))

        # Check if channel is waiting for confirmation
        if channel_id not in confirmation_pending:
            await event.respond("❌ Ce canal n'est pas en attente de configuration")
            return

        detected_display_channel = channel_id
        confirmation_pending[channel_id] = 'configured_display'

        # Save configuration
        save_config()

        try:
            chat = await client.get_entity(channel_id)
            chat_title = getattr(chat, 'title', f'Canal {channel_id}')
        except:
            chat_title = f'Canal {channel_id}'

        await event.respond(f"✅ **Canal de diffusion configuré**\n📋 {chat_title}\n\n🚀 Le bot publiera les prédictions dans ce canal - développé par Sossou Kouamé Appolinaire\n💾 Configuration sauvegardée automatiquement")
        print(f"Canal de diffusion configuré: {channel_id}")

    except Exception as e:
        print(f"Erreur dans set_display_channel: {e}")

@client.on(events.NewMessage(pattern=r'/force_set_display (-?\d+)'))
async def force_set_display_channel(event):
    """Force set display channel without waiting for invitation (admin only)"""
    global detected_display_channel

    try:
        # Only allow admin
        if ADMIN_ID and event.sender_id != ADMIN_ID:
            await event.respond("❌ Seul l'administrateur peut configurer les canaux")
            return

        # Extract channel ID from command
        match = event.pattern_match
        channel_id = int(match.group(1))

        detected_display_channel = channel_id

        # Save configuration
        save_config()

        try:
            chat = await client.get_entity(channel_id)
            chat_title = getattr(chat, 'title', f'Canal {channel_id}')
        except:
            chat_title = f'Canal {channel_id}'

        await event.respond(f"✅ **Canal de diffusion configuré (force)**\n📋 {chat_title}\n🆔 ID: {channel_id}\n\n🚀 Le bot publiera les prédictions dans ce canal\n💾 Configuration sauvegardée automatiquement")
        print(f"Canal de diffusion configuré (force): {channel_id}")

    except Exception as e:
        print(f"Erreur dans force_set_display_channel: {e}")
        await event.respond(f"❌ Erreur: {e}")

@client.on(events.NewMessage(pattern=r'/a\s*(\d+)?'))
async def set_a_offset(event):
    """Set or show the prediction offset value (N+a)"""
    global a_offset
    
    try:
        if ADMIN_ID and event.sender_id != ADMIN_ID:
            await event.respond("❌ Seul l'administrateur peut modifier ce paramètre")
            return
        
        match = event.pattern_match
        new_value = match.group(1)
        
        if new_value:
            a_offset = int(new_value)
            save_config()
            await event.respond(f"✅ **Décalage de prédiction mis à jour**\n\n📊 Nouvelle valeur: **a = {a_offset}**\n\n🎯 Les prédictions seront: N + {a_offset}\n💾 Configuration sauvegardée")
            print(f"Décalage a_offset mis à jour: {a_offset}")
        else:
            await event.respond(f"📊 **Décalage actuel: a = {a_offset}**\n\n🎯 Les prédictions sont: N + {a_offset}\n\n💡 Pour modifier: `/a [valeur]`\nExemple: `/a 3` pour N+3")
    
    except Exception as e:
        print(f"Erreur dans set_a_offset: {e}")
        await event.respond(f"❌ Erreur: {e}")

@client.on(events.NewMessage(pattern=r'/r\s*(\d+)?'))
async def set_r_offset(event):
    """Set or show the verification offset value (r)"""
    global r_offset
    
    try:
        if ADMIN_ID and event.sender_id != ADMIN_ID:
            await event.respond("❌ Seul l'administrateur peut modifier ce paramètre")
            return
        
        match = event.pattern_match
        new_value = match.group(1)
        
        if new_value:
            value = int(new_value)
            if value < 0 or value > 10:
                await event.respond("❌ **Valeur invalide**\n\nL'offset de vérification doit être entre **0** et **10**.\n\n💡 Exemple: `/r 2` pour vérifier N+0, N+1, N+2")
                return
            
            r_offset = value
            save_config()
            
            emoji_list = "\n".join([f"• N+{i}: {VERIFICATION_EMOJIS[i]}" for i in range(0, r_offset + 1)])
            
            await event.respond(f"""✅ **Offset de vérification mis à jour**

📊 Nouvelle valeur: **r = {r_offset}**

🎯 Vérification de N+0 à N+{r_offset}

**Emojis de vérification:**
{emoji_list}

💡 Note: L'emoji indique le nombre d'essais après la première vérification
   (0 = succès au 1er essai, 1 = succès au 2ème essai, etc.)

💾 Configuration sauvegardée""")
            print(f"Offset r_offset mis à jour: {r_offset}")
        else:
            emoji_list = "\n".join([f"• N+{i}: {VERIFICATION_EMOJIS[i]}" for i in range(0, r_offset + 1)])
            
            await event.respond(f"""📊 **Offset de vérification actuel: r = {r_offset}**

🎯 Vérification de N+0 à N+{r_offset}

**Emojis de vérification:**
{emoji_list}

💡 Pour modifier: `/r [valeur]` (0-10)
Exemple: `/r 2` pour vérifier N+0, N+1, N+2""")
    
    except Exception as e:
        print(f"Erreur dans set_r_offset: {e}")
        await event.respond(f"❌ Erreur: {e}")

# --- FONCTIONS D'ANALYSE DES MESSAGES DU CANAL SOURCE ---

def extract_card_value(card: str) -> str:
    """Extrait la valeur d'une carte (A, K, Q, J, 10, 9, 8, 7, 6, 5, 4, 3, 2)"""
    card_values = ['10', 'A', 'K', 'Q', 'J', '9', '8', '7', '6', '5', '4', '3', '2']
    for val in card_values:
        if val in card:
            return val
    return ""

def has_six_in_first_group(message_text: str) -> bool:
    """
    Vérifie si le premier groupe de cartes contient une carte de valeur 6.
    Exemple: A♠️6♠️ contient un 6, mais 3♠️3♠️ ne contient pas de 6.
    """
    try:
        pattern = r"[✅🔰]?\d+\(([^)]+)\)"
        matches = re.findall(pattern, message_text)
        if matches and len(matches) >= 1:
            first_group = matches[0]
            card_pattern = r'(\d+|[AKQJ])[♠️♥️♦️♣️♠♥♦♣]'
            cards = re.findall(card_pattern, first_group)
            for card_value in cards:
                if card_value == '6':
                    print(f"✅ Trouvé une carte 6 dans le premier groupe: {first_group}")
                    return True
            print(f"ℹ️ Pas de carte 6 dans le premier groupe: {first_group} (cartes: {cards})")
        return False
    except Exception as e:
        print(f"Erreur has_six_in_first_group: {e}")
        return False

def has_six_in_both_groups(message_text: str) -> bool:
    """
    Vérifie si CHAQUE groupe (premier ET second) contient au moins une carte de valeur 6.
    Retourne True si les deux groupes contiennent chacun au moins un 6.
    """
    try:
        pattern = r"[✅🔰]?\d+\(([^)]+)\)"
        matches = re.findall(pattern, message_text)
        
        if len(matches) < 2:
            return False
        
        # Vérifier le premier groupe
        first_group = matches[0]
        card_pattern = r'(\d+|[AKQJ])[♠️♥️♦️♣️♠♥♦♣]'
        first_group_cards = re.findall(card_pattern, first_group)
        has_six_in_first = any(card_value == '6' for card_value in first_group_cards)
        
        # Vérifier le second groupe
        second_group = matches[1]
        second_group_cards = re.findall(card_pattern, second_group)
        has_six_in_second = any(card_value == '6' for card_value in second_group_cards)
        
        if has_six_in_first and has_six_in_second:
            print(f"⚠️ EXCLUSION: Premier groupe contient '6' ET second groupe contient '6'")
            print(f"   Premier groupe: {first_group} (cartes: {first_group_cards})")
            print(f"   Second groupe: {second_group} (cartes: {second_group_cards})")
            return True
        
        return False
    except Exception as e:
        print(f"Erreur has_six_in_both_groups: {e}")
        return False

def count_sixes_in_groups(message_text: str) -> int:
    """
    Compte le nombre total de cartes de valeur 6 dans tous les groupes.
    Retourne le nombre total de '6' trouvés.
    """
    try:
        pattern = r"[✅🔰]?\d+\(([^)]+)\)"
        matches = re.findall(pattern, message_text)
        total_sixes = 0
        
        for group in matches:
            card_pattern = r'(\d+|[AKQJ])[♠️♥️♦️♣️♠♥♦♣]'
            cards = re.findall(card_pattern, group)
            sixes_in_group = sum(1 for card_value in cards if card_value == '6')
            total_sixes += sixes_in_group
        
        print(f"📊 Nombre total de '6' trouvés dans tous les groupes: {total_sixes}")
        return total_sixes
    except Exception as e:
        print(f"Erreur count_sixes_in_groups: {e}")
        return 0

def get_first_group_total(message_text: str) -> int:
    """Extrait le total du premier groupe (le chiffre avant les parenthèses)"""
    try:
        pattern = r"[✅🔰]?(\d+)\(([^)]+)\)"
        matches = re.findall(pattern, message_text)
        if matches and len(matches) >= 1:
            total = int(matches[0][0])
            print(f"📊 Total du premier groupe: {total}")
            return total
        return -1
    except Exception as e:
        print(f"Erreur get_first_group_total: {e}")
        return -1

def extract_t_value(message_text: str) -> float:
    """Extrait la valeur #T du message"""
    try:
        match = re.search(r'#T(\d+(?:\.\d+)?)', message_text)
        if match:
            t_value = float(match.group(1))
            print(f"📊 Valeur #T extraite: {t_value}")
            return t_value
        return -1
    except Exception as e:
        print(f"Erreur extract_t_value: {e}")
        return -1

def is_tie_game(message_text: str) -> bool:
    """
    Vérifie si c'est un match nul.
    Format match nul: les deux groupes ont le même score et 🟣#X est présent
    Exemple: #N25. 5(Q♣️6♥️5♣️) 🔰 5(3♣️9♦️3♠️) #T10 🟣#X
    """
    try:
        if '🟣#X' in message_text:
            print("🔰 Match nul détecté (🟣#X présent) - pas de prédiction")
            return True
        return False
    except Exception as e:
        print(f"Erreur is_tie_game: {e}")
        return False

def should_skip_prediction(message_text: str) -> bool:
    """
    Vérifie si on doit ignorer la prédiction:
    - Match nul (🔰 entre groupes avec 🟣#X)
    - Premier groupe total = 6 ET contient un 6 dans les cartes
    - 2 valeurs '6' ou plus dans tous les groupes combinés
    - Premier groupe contient un 6 ET second groupe contient un 6
    """
    if is_tie_game(message_text):
        return True
    
    # Vérifier si les deux groupes contiennent chacun au moins un 6
    if has_six_in_both_groups(message_text):
        print(f"⚠️ Les deux groupes contiennent chacun une carte '6' - pas de prédiction")
        return True
    
    # Vérifier s'il y a 2 valeurs '6' ou plus
    total_sixes = count_sixes_in_groups(message_text)
    if total_sixes >= 2:
        print(f"⚠️ Trouvé {total_sixes} cartes '6' dans les groupes - pas de prédiction")
        return True
    
    first_group_total = get_first_group_total(message_text)
    has_six = has_six_in_first_group(message_text)
    
    if first_group_total == 6 and has_six:
        print(f"⚠️ Total premier groupe = 6 ET contient un 6 - pas de prédiction")
        return True
    
    return False

def is_finalized_message(message_text: str) -> bool:
    """Vérifie si le message est finalisé (✅ ou 🔰)"""
    return '✅' in message_text or '🔰' in message_text

async def verify_active_predictions(game_number: int, message_text: str):
    """
    Vérifie les prédictions actives basées sur les messages du canal source.
    
    Logique de vérification séquentielle:
    1. Vérifie d'abord à N+0 (numéro exact prédit)
    2. Si échec et r ≥ 1, continue à N+1
    3. Si échec et r ≥ 2, continue à N+2
    4. Marque ❌ si échec après tous les essais autorisés par r_offset
    """
    global active_predictions
    
    if not is_finalized_message(message_text):
        return
    
    for pred_numero_str in list(active_predictions.keys()):
        pred_numero = int(pred_numero_str)
        pred_data = active_predictions[pred_numero_str]
        
        # Ignorer si déjà vérifiée
        if pred_data.get("verified", False):
            continue
        
        # Récupérer le nombre d'essais déjà effectués
        attempts_done = pred_data.get("attempts", 0)
        
        # Si le jeu actuel est avant notre prédiction, ignorer
        if game_number < pred_numero:
            continue
        
        # Calculer l'offset actuel (combien de jeux après la prédiction)
        current_offset = game_number - pred_numero
        
        # Si on a dépassé le nombre maximum d'essais autorisés, marquer comme échec
        if current_offset > r_offset:
            msg_id = pred_data.get("message_id")
            channel_id = pred_data.get("channel_id")
            base_text = pred_data.get("base_text", "")
            
            if msg_id and channel_id:
                new_text = base_text.replace("statut :⏳", "statut :❌")
                try:
                    await client.edit_message(channel_id, msg_id, new_text)
                    print(f"❌ Prédiction #{pred_numero} expirée après offset {r_offset}")
                except Exception as e:
                    print(f"❌ Erreur mise à jour prédiction expirée #{pred_numero}: {e}")
            
            pred_data["verified"] = True
            pred_data["status"] = "❌"
            pred_data["attempts"] = r_offset + 1
            save_config()
            continue
        
        # Vérifier seulement si c'est un offset qu'on n'a pas encore testé
        if current_offset > attempts_done:
            msg_id = pred_data.get("message_id")
            channel_id = pred_data.get("channel_id")
            expected = pred_data.get("expected", "")
            
            if not msg_id or not channel_id:
                continue
            
            # Extraire le point du premier groupe
            premier_groupe_point, _ = excel_manager.extract_points_and_winner(message_text)
            
            if premier_groupe_point is None:
                print(f"⚠️ Impossible d'extraire le point du premier groupe du jeu #{game_number}")
                continue
            
            # Vérifier si la prédiction est réussie
            is_success = False
            if expected == "joueur":
                # P+6,5 : succès si point > 6.5
                if premier_groupe_point > 6.5:
                    is_success = True
                    print(f"✅ Prédiction #{pred_numero} JOUEUR (P+6,5) réussie à N+{current_offset}: point={premier_groupe_point} > 6.5")
            elif expected == "banquier":
                # M-4,5 : succès si point < 4.5
                if premier_groupe_point < 4.5:
                    is_success = True
                    print(f"✅ Prédiction #{pred_numero} BANQUIER (M-4,5) réussie à N+{current_offset}: point={premier_groupe_point} < 4.5")
            
            # Mettre à jour le nombre d'essais
            pred_data["attempts"] = current_offset
            
            if is_success:
                # Succès: marquer avec l'emoji approprié et arrêter
                status_emoji = VERIFICATION_EMOJIS.get(current_offset, f"✅{current_offset}")
                base_text = pred_data.get("base_text", "")
                new_text = base_text.replace("statut :⏳", f"statut :{status_emoji}")
                
                try:
                    await client.edit_message(channel_id, msg_id, new_text)
                    pred_data["verified"] = True
                    pred_data["status"] = status_emoji
                    save_config()
                    print(f"✅ Prédiction #{pred_numero} validée: {status_emoji} (N+{current_offset})")
                except Exception as e:
                    print(f"❌ Erreur mise à jour prédiction #{pred_numero}: {e}")
            else:
                # Échec sur cet essai
                print(f"⏳ Prédiction #{pred_numero} échec à N+{current_offset} (essai {current_offset + 1}/{r_offset + 1})")
                
                # Si c'est le dernier essai autorisé, marquer comme échec définitif
                if current_offset >= r_offset:
                    base_text = pred_data.get("base_text", "")
                    new_text = base_text.replace("statut :⏳", "statut :❌")
                    
                    try:
                        await client.edit_message(channel_id, msg_id, new_text)
                        pred_data["verified"] = True
                        pred_data["status"] = "❌"
                        save_config()
                        print(f"❌ Prédiction #{pred_numero} échouée après tous les essais (N+0 à N+{r_offset})")
                    except Exception as e:
                        print(f"❌ Erreur mise à jour prédiction #{pred_numero}: {e}")
                else:
                    # Continuer à surveiller pour le prochain offset
                    save_config()

async def verify_excel_predictions(game_number: int, message_text: str):
    """Fonction consolidée pour vérifier toutes les prédictions Excel en attente"""
    for key, pred in list(excel_manager.predictions.items()):
        # Ignorer si pas lancée ou déjà vérifiée
        if not pred["launched"] or pred.get("verified", False):
            continue

        pred_numero = pred["numero"]
        expected_winner = pred["victoire"]
        current_offset = pred.get("current_offset", 0)
        target_number = pred_numero + current_offset

        # DÉTECTION DE SAUT DE NUMÉRO
        if game_number > target_number:
            print(f"⚠️ Numéro sauté: #{pred_numero} attendait #{target_number}, reçu #{game_number}")

            while current_offset <= 2 and game_number > pred_numero + current_offset:
                current_offset += 1
                print(f"⏭️ Prédiction #{pred_numero}: saut à offset {current_offset}")

            # Note: excel_manager.verify_excel_prediction gère maintenant la vérification d'échec > 2
            if current_offset > 2:
                # Marquer comme échec si l'offset dépasse 2
                await update_prediction_status(pred, pred_numero, expected_winner, "❌", True) # MODIFIÉ : "⭕✍🏻" -> "❌"
                continue
            else:
                pred["current_offset"] = current_offset
                excel_manager.save_predictions()

        # Vérification séquentielle
        status, should_continue = excel_manager.verify_excel_prediction(
            game_number, message_text, pred_numero, expected_winner, current_offset
        )

        if status:
            await update_prediction_status(pred, pred_numero, expected_winner, status, True)
        elif should_continue and game_number == pred_numero + current_offset:
            new_offset = current_offset + 1
            if new_offset <= 2:
                pred["current_offset"] = new_offset
                excel_manager.save_predictions()
                print(f"⏭️ Prédiction #{pred_numero}: offset {new_offset}")
            else:
                # Échec définitif après offset 2 non réussi
                await update_prediction_status(pred, pred_numero, expected_winner, "❌", True) # MODIFIÉ : "⭕✍🏻" -> "❌"

async def update_prediction_status(pred: dict, numero: int, winner: str, status: str, verified: bool):
    """Mise à jour unifiée du statut de prédiction"""
    msg_id = pred.get("message_id")
    channel_id = pred.get("channel_id")

    if msg_id and channel_id:
        # Utiliser la nouvelle fonction (qui prend numero et winner) pour obtenir le format complet (incluant statut :⏳)
        full_base_text_with_placeholder = excel_manager.get_prediction_format(numero, winner)

        # Le format complet est: 🔵{numero}:🅿️+6,5🔵statut :⏳
        # Nous devons remplacer la fin :⏳ par :{status}

        # Sépare le texte avant 'statut :⏳' et prend la première partie
        base_format = full_base_text_with_placeholder.rsplit("statut :⏳", 1)[0]

        # Reconstruit le message avec le nouveau statut
        new_text = f"{base_format}statut :{status}"

        try:
            await client.edit_message(channel_id, msg_id, new_text)
            pred["verified"] = verified
            excel_manager.save_predictions()
            print(f"✅ Prédiction #{numero} mise à jour: {status}")
        except Exception as e:
            print(f"❌ Erreur mise à jour #{numero}: {e}")


# --- COMMANDES DE BASE ---
@client.on(events.NewMessage(pattern='/start'))
async def start_command(event):
    """Send welcome message when user starts the bot"""
    try:
        welcome_msg = f"""🎯 **Bot de Prédiction de Cartes - Bienvenue !**

🔹 **Développé par Sossou Kouamé Appolinaire**

**Fonctionnalités** :
• 🔍 Surveillance automatique du canal source
• 🎯 Détection automatique du "6" dans le premier groupe
• 📊 Prédiction basée sur #T (>10.5 = Joueur, ≤10.5 = Banquier)
• ✅ Vérification automatique des résultats

**Configuration** :
1. Ajoutez-moi dans vos canaux
2. Je vous enverrai automatiquement une invitation privée
3. Répondez avec `/set_stat [ID]` ou `/set_display [ID]`

**Commandes Admin** :
• `/start` - Ce message
• `/status` - État du bot
• `/a [valeur]` - Définir le décalage (N+a) [actuel: {a_offset}]
• `/sta` - Statistiques des prédictions
• `/reset` - Réinitialiser toutes les données
• `/ni` - Informations système
• `/set_stat [ID]` - Configurer canal source
• `/set_display [ID]` - Configurer canal diffusion
• `/force_set_stat [ID]` - Forcer config canal source
• `/force_set_display [ID]` - Forcer config canal diffusion

**Logique de prédiction** :
1. Détection d'un "6" dans le premier groupe de cartes
2. Vérification que #T existe
3. Si #T > 10.5 → 🔵N+a:🅿️+6,5🔵statut :⏳ (Joueur)
4. Si #T ≤ 10.5 → 🔵N+a:Ⓜ️-4,,5🔵statut :⏳ (Banquier)

**Exclusions** :
• Match nul (🔰 entre groupes avec 🟣#X)
• Total premier groupe = 6 ET carte 6 présente

Le bot est prêt à analyser vos jeux ! 🚀"""

        await event.respond(welcome_msg)
        print(f"Message de bienvenue envoyé à l'utilisateur {event.sender_id}")

        # Test message private pour vérifier la connectivité
        if event.sender_id == ADMIN_ID:
            await asyncio.sleep(2)
            test_msg = "🔧 Test de connectivité : Je peux vous envoyer des messages privés !"
            await event.respond(test_msg)

    except Exception as e:
        print(f"Erreur dans start_command: {e}")

# --- COMMANDES ADMINISTRATIVES ---
@client.on(events.NewMessage(pattern='/status'))
async def show_status(event):
    """Show bot status (admin only)"""
    try:
        # Permettre si ADMIN_ID est configuré ou en mode développement
        if ADMIN_ID and event.sender_id != ADMIN_ID:
            return

        # Recharger la configuration pour éviter les valeurs obsolètes
        load_config()

        config_status = "✅ Sauvegardée" if os.path.exists(CONFIG_FILE) else "❌ Non sauvegardée"
        status_msg = f"""📊 **Statut du Bot**

Canal statistiques: {'✅ Configuré' if detected_stat_channel else '❌ Non configuré'} ({detected_stat_channel})
Canal diffusion: {'✅ Configuré' if detected_display_channel else '❌ Non configuré'} ({detected_display_channel})
⏱️ Intervalle de prédiction: {prediction_interval} minutes
Configuration persistante: {config_status}
Prédictions actives: {len(predictor.prediction_status)}
Dernières prédictions: {len(predictor.last_predictions)}
"""
        await event.respond(status_msg)
    except Exception as e:
        print(f"Erreur dans show_status: {e}")

@client.on(events.NewMessage(pattern='/reset'))
async def reset_data(event):
    """Réinitialisation des données (admin uniquement)"""
    try:
        if event.sender_id != ADMIN_ID:
            return

        # Réinitialiser les données du predictor
        predictor.reset()

        # Réinitialiser les données YAML
        db.reset_all_data()

        msg = """🔄 **Données réinitialisées avec succès !**

✅ Prédictions en attente: vidées
✅ Base de données YAML: réinitialisée
✅ Configuration: préservée

Le bot est prêt pour un nouveau cycle."""

        await event.respond(msg)
        print(f"Données réinitialisées par l'admin")

    except Exception as e:
        print(f"Erreur dans reset_data: {e}")
        await event.respond(f"❌ Erreur lors de la réinitialisation: {e}")

@client.on(events.NewMessage(pattern='/ni'))
async def ni_command(event):
    """Commande /ni - Informations sur le système de prédiction"""
    try:
        # Utiliser les variables globales configurées
        stats_channel = detected_stat_channel or 'Non configuré'
        display_channel = detected_display_channel or 'Non configuré'

        # Compter les prédictions actives depuis le predictor
        active_predictions = len([s for s in predictor.prediction_status.values() if s == '⌛'])

        msg = f"""🎯 **Système de Prédiction NI - Statut**

📊 **Configuration actuelle**:
• Canal source: {stats_channel}
• Canal affichage: {display_channel}
• Prédictions Excel actives: {active_predictions}
• Intervalle: {prediction_interval} minute(s)

🎮 **Fonctionnalités**:
• Prédictions basées uniquement sur fichier Excel
• Vérification séquentielle avec offsets 0→1→2
• Format Joueur: "🔵XXX:🅿️+6,5🔵statut :⏳"
• Format Banquier: "🔵XXX:Ⓜ️-4,,5🔵statut :⏳"

🔧 **Commandes disponibles**:
• `/set_stat [ID]` - Configurer canal source
• `/set_display [ID]` - Configurer canal affichage
• `/excel_status` - Voir prédictions Excel
• `/reset` - Réinitialiser les données
• `/deploy` - Créer package de déploiement

✅ **Bot opérationnel** - Version 2025"""

        await event.respond(msg)
        print(f"Commande /ni exécutée par {event.sender_id}")

    except Exception as e:
        print(f"Erreur dans ni_command: {e}")
        await event.respond(f"❌ Erreur: {e}")

@client.on(events.NewMessage(pattern='/deploy'))
async def deploy_command(event):
    """Créer un package zip de déploiement avec tous les fichiers à la racine"""
    try:
        if ADMIN_ID and event.sender_id != ADMIN_ID:
            await event.respond("❌ Seul l'administrateur peut créer un package de déploiement")
            return

        await event.respond("📦 **Création du package fin2025 en cours...**")

        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        zip_filename = f"fin2025_{timestamp}.zip"

        # Liste des fichiers à inclure (tous à la racine)
        files_to_include = [
            'main.py', 'predictor.py', 'excel_importer.py', 'yaml_manager.py',
            'requirements.txt', 'bot_config.json', 'Procfile', 'render.yaml'
        ]

        with zipfile.ZipFile(zip_filename, 'w', zipfile.ZIP_DEFLATED) as zipf:
            for file in files_to_include:
                if os.path.exists(file):
                    zipf.write(file, file)  # Fichier à la racine du zip

        if os.path.exists(zip_filename):
            file_size = os.path.getsize(zip_filename) / (1024 * 1024)
            
            await client.send_file(
                event.chat_id,
                zip_filename,
                caption=f"📦 **Package fin2025 créé avec succès!**\n\n✅ Fichier: {zip_filename}\n💾 Taille: {file_size:.2f} MB\n🎯 Tous les fichiers à la racine\n🚀 Prêt pour déploiement Replit"
            )
            
            try:
                os.remove(zip_filename)
            except:
                pass
            
            print(f"✅ Package {zip_filename} créé et envoyé")
        else:
            await event.respond("❌ Erreur: Impossible de créer le fichier zip")
            
    except Exception as e:
        print(f"❌ Erreur deploy_command: {e}")
        await event.respond(f"❌ Erreur: {e}")


@client.on(events.NewMessage(pattern='/test_invite'))
async def test_invite(event):
    """Test sending invitation (admin only)"""
    try:
        if event.sender_id != ADMIN_ID:
            return

        # Test invitation message
        test_msg = f"""🔔 **Test d'invitation**

📋 **Canal test** : Canal de test
🆔 **ID** : -1001234567890

**Choisissez le type de canal** :
• `/set_stat -1001234567890` - Canal de statistiques
• `/set_display -1001234567890` - Canal de diffusion

Ceci est un message de test pour vérifier les invitations."""

        await event.respond(test_msg)
        print(f"Message de test envoyé à l'admin")

    except Exception as e:
        print(f"Erreur dans test_invite: {e}")

@client.on(events.NewMessage(pattern='/sta'))
async def show_excel_stats(event):
    """Show Excel predictions statistics"""
    try:
        if ADMIN_ID and event.sender_id != ADMIN_ID:
            return

        # Recharger la configuration pour éviter les valeurs obsolètes
        load_config()

        stats = excel_manager.get_stats()

        msg = f"""📊 **Statut des Prédictions Excel**

📋 **Statistiques Excel**:
• Total prédictions: {stats['total']}
• En attente: {stats['pending']}
• Lancées: {stats['launched']}

📈 **Configuration actuelle**:
• Canal stats configuré: {'✅' if detected_stat_channel else '❌'} ({detected_stat_channel or 'Aucun'})
• Canal affichage configuré: {'✅' if detected_display_channel else '❌'} ({detected_display_channel or 'Aucun'})

🔧 **Format de prédiction**:
• Joueur (P+6,5) : 🔵XXX:🅿️+6,5🔵statut :⏳
• Banquier (M-4,5) : 🔵XXX:Ⓜ️-4,,5🔵statut :⏳

✅ Prédictions uniquement depuis fichier Excel"""

        await event.respond(msg)
        print(f"Statut Excel envoyé à l'admin")

    except Exception as e:
        print(f"Erreur dans show_excel_stats: {e}")
        await event.respond(f"❌ Erreur: {e}")

@client.on(events.NewMessage(pattern='/excel_clear'))
async def clear_excel_predictions(event):
    """Effacer toutes les prédictions Excel"""
    try:
        if ADMIN_ID and event.sender_id != ADMIN_ID:
            return

        old_count = len(excel_manager.predictions)
        excel_manager.predictions.clear()
        excel_manager.save_predictions()

        msg = f"""🗑️ **Prédictions Excel effacées**

✅ {old_count} prédictions supprimées
📋 La base est maintenant vide

Vous pouvez importer un nouveau fichier Excel."""

        await event.respond(msg)
        print(f"Prédictions Excel effacées par l'admin: {old_count} entrées")

    except Exception as e:
        print(f"Erreur dans clear_excel_predictions: {e}")
        await event.respond(f"❌ Erreur: {e}")

# Commande /report et /scheduler supprimées (non utilisées)

@client.on(events.NewMessage(func=lambda e: e.is_private and e.document))
async def handle_excel_document(event):
    """Détecte automatiquement les fichiers Excel envoyés par l'admin (sans commande)"""
    try:
        if ADMIN_ID and event.sender_id != ADMIN_ID:
            return

        if not event.message.file:
            return

        mime_type = event.message.file.mime_type or ""
        file_name = event.message.file.name or ""

        excel_mimes = [
            'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            'application/vnd.ms-excel',
            'application/octet-stream'
        ]
        excel_extensions = ['.xlsx', '.xls']

        is_excel = any(mime in mime_type for mime in excel_mimes) or any(file_name.lower().endswith(ext) for ext in excel_extensions)

        if not is_excel:
            return

        print(f"📥 Fichier Excel détecté via Telegram: {file_name}")
        await event.respond("📥 **Fichier Excel détecté! Téléchargement en cours...**")

        file_path = await event.message.download_media()

        if not file_path:
            await event.respond("❌ **Erreur**: Impossible de télécharger le fichier.")
            return

        await event.respond("⚙️ **Importation des prédictions...**")

        old_count = len(excel_manager.predictions)
        result = excel_manager.import_excel(file_path, replace_mode=True)

        try:
            os.remove(file_path)
        except:
            pass

        if result["success"]:
            stats = excel_manager.get_stats()
            consecutive_info = result.get('consecutive_skipped', 0)

            msg = f"""📥 Import Excel via Telegram

✅ Fichier Excel importé avec succès!
• Prédictions importées: {result['imported']}
• Anciennes remplacées: {old_count}
• Consécutifs ignorés: {consecutive_info}
• Total en base: {stats['total']}

Le système est prêt pour les prédictions! 🎉

📋 **Statistiques**:
• En attente: {stats['pending']}
• Lancées: {stats['launched']}"""

            await event.respond(msg)
            print(f"✅ Import Excel via Telegram réussi: {result['imported']} prédictions")
        else:
            await event.respond(f"❌ **Erreur importation Excel**: {result.get('error', 'Erreur inconnue')}")
            print(f"❌ Erreur importation Excel: {result.get('error')}")

    except Exception as e:
        print(f"Erreur dans handle_excel_document: {e}")
        await event.respond(f"❌ **Erreur critique**: {e}")

@client.on(events.NewMessage(pattern=r'/upload_excel', func=lambda e: e.is_private and e.sender_id == ADMIN_ID and e.media))
async def handle_excel_upload(event):
    """Handle Excel file upload from admin in private chat (legacy command)"""
    pass

# --- LOGIQUE PRINCIPALE : ÉCOUTE DU CANAL SOURCE ---

@client.on(events.NewMessage())
@client.on(events.MessageEdited())
async def handle_new_message(event):
    """
    Gère les nouveaux messages ET les messages édités dans le canal de statistiques.
    
    Nouvelle logique de prédiction:
    1. Détecte si le premier groupe contient un "6" dans les cartes
    2. Si oui, vérifie la valeur #T
    3. Si #T > 10.5 → prédit Joueur (🅿️+6,5)
    4. Si #T <= 10.5 → prédit Banquier (Ⓜ️-4,,5)
    5. Ignore les matchs nuls et les cas où total=6 ET carte=6
    """
    global active_predictions
    
    if not detected_stat_channel:
        return
    if not (event.is_channel and event.chat_id == detected_stat_channel):
        return
    
    message_text = event.raw_text
    game_number = predictor.extract_game_number(message_text)
    
    if not game_number:
        return
    
    print(f"📨 Message reçu du canal source - Jeu #{game_number}")
    
    # --- ÉTAPE 1: VÉRIFICATION DES PRÉDICTIONS ACTIVES ---
    await verify_active_predictions(game_number, message_text)
    
    # --- ÉTAPE 2: NOUVELLE PRÉDICTION BASÉE SUR LA DÉTECTION DU 6 ---
    if not detected_display_channel:
        print(f"⚠️ Canal de diffusion non configuré - impossible de lancer des prédictions")
        return
    
    # Vérifier si le message est finalisé (✅ ou 🔰)
    if not is_finalized_message(message_text):
        print(f"⏳ Message #{game_number} pas encore finalisé - en attente")
        return
    
    # Vérifier si on doit ignorer ce message
    if should_skip_prediction(message_text):
        print(f"⏭️ Message #{game_number} ignoré (match nul ou total=6 avec carte 6)")
        return
    
    # Vérifier si le premier groupe contient un 6
    if not has_six_in_first_group(message_text):
        print(f"ℹ️ Pas de 6 dans le premier groupe du jeu #{game_number} - pas de prédiction")
        return
    
    # Extraire la valeur #T
    t_value = extract_t_value(message_text)
    if t_value < 0:
        print(f"⚠️ Impossible d'extraire #T du jeu #{game_number}")
        return
    
    # Calculer le numéro de prédiction: N + a
    predicted_numero = game_number + a_offset
    
    # Vérifier si une prédiction existe déjà pour ce numéro
    if str(predicted_numero) in active_predictions:
        print(f"ℹ️ Prédiction #{predicted_numero} déjà existante - ignorée")
        return
    
    # Déterminer le type de prédiction
    if t_value > 10.5:
        prediction_type = "joueur"
        prediction_text = f"🔵{predicted_numero}:🅿️+6,5🔵statut :⏳"
        print(f"🎯 #T={t_value} > 10.5 → Prédiction JOUEUR pour #{predicted_numero}")
    else:
        prediction_type = "banquier"
        prediction_text = f"🔵{predicted_numero}:Ⓜ️-4,,5🔵statut :⏳"
        print(f"🎯 #T={t_value} <= 10.5 → Prédiction BANQUIER pour #{predicted_numero}")
    
    # Envoyer la prédiction
    try:
        sent_message = await client.send_message(detected_display_channel, prediction_text)
        
        # Enregistrer la prédiction active
        active_predictions[str(predicted_numero)] = {
            "message_id": sent_message.id,
            "channel_id": detected_display_channel,
            "expected": prediction_type,
            "base_text": prediction_text,
            "source_game": game_number,
            "t_value": t_value,
            "verified": False,
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }
        save_config()
        
        print(f"✅ Prédiction lancée: {prediction_text} (source: #{game_number}, #T={t_value})")
        
    except Exception as e:
        print(f"❌ Erreur envoi prédiction: {e}")

# --- DÉTECTION AUTOMATIQUE DES FICHIERS EXCEL ---

def get_excel_files_in_project():
    """Retourne la liste des fichiers Excel dans le répertoire du projet"""
    excel_patterns = ["*.xlsx", "*.xls"]
    excel_files = []
    for pattern in excel_patterns:
        excel_files.extend(glob.glob(os.path.join(EXCEL_WATCH_DIR, pattern)))
    return excel_files

def load_processed_files():
    """Charge la liste des fichiers déjà traités depuis un fichier de persistance"""
    global processed_excel_files
    try:
        processed_file = "processed_excel_files.json"
        if os.path.exists(processed_file):
            with open(processed_file, 'r') as f:
                data = json.load(f)
                processed_excel_files = set(data.get('files', []))
    except Exception as e:
        print(f"⚠️ Erreur chargement fichiers traités: {e}")
        processed_excel_files = set()

def save_processed_files():
    """Sauvegarde la liste des fichiers traités"""
    try:
        processed_file = "processed_excel_files.json"
        with open(processed_file, 'w') as f:
            json.dump({'files': list(processed_excel_files)}, f)
    except Exception as e:
        print(f"⚠️ Erreur sauvegarde fichiers traités: {e}")

async def check_new_excel_files():
    """Vérifie s'il y a de nouveaux fichiers Excel dans le projet"""
    global processed_excel_files

    try:
        current_files = get_excel_files_in_project()

        for file_path in current_files:
            file_name = os.path.basename(file_path)
            file_mtime = os.path.getmtime(file_path)
            file_key = f"{file_name}_{file_mtime}"

            if file_key not in processed_excel_files:
                print(f"📥 Nouveau fichier Excel détecté: {file_name}")
                await auto_import_excel(file_path)
                processed_excel_files.add(file_key)
                save_processed_files()

    except Exception as e:
        print(f"⚠️ Erreur vérification fichiers Excel: {e}")

async def auto_import_excel(file_path: str):
    """Importe automatiquement un fichier Excel et envoie la confirmation à l'admin"""
    try:
        file_name = os.path.basename(file_path)
        print(f"📥 Import Automatique: {file_name}")

        old_count = len(excel_manager.predictions)
        result = excel_manager.import_excel(file_path, replace_mode=True)

        if result["success"]:
            stats = excel_manager.get_stats()
            consecutive_info = result.get('consecutive_skipped', 0)

            msg = f"""📥 Import Automatique dans Projet

✅ Fichier Excel importé avec succès!
• Prédictions importées: {result['imported']}
• Anciennes remplacées: {old_count}
• Consécutifs ignorés: {consecutive_info}
• Total en base: {stats['total']}

Le système est prêt pour la nouvelle journée! 🎉"""

            print(msg)

            if ADMIN_ID:
                try:
                    await client.send_message(ADMIN_ID, msg)
                    print(f"✅ Message de confirmation envoyé à l'admin")
                except Exception as e:
                    print(f"⚠️ Impossible d'envoyer le message à l'admin: {e}")
        else:
            error_msg = f"❌ Erreur import Excel automatique: {result.get('error', 'Erreur inconnue')}"
            print(error_msg)
            if ADMIN_ID:
                try:
                    await client.send_message(ADMIN_ID, error_msg)
                except:
                    pass

    except Exception as e:
        print(f"❌ Erreur import automatique: {e}")

async def excel_file_watcher():
    """Boucle de surveillance des fichiers Excel (toutes les 10 secondes)"""
    load_processed_files()
    print("👀 Surveillance des fichiers Excel activée")

    while True:
        try:
            await check_new_excel_files()
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            break
        except Exception as e:
            print(f"⚠️ Erreur dans le watcher Excel: {e}")
            await asyncio.sleep(30)

# --- FONCTIONS UTILITAIRES POUR LE SERVEUR WEB ---

async def health_check(request):
    """Simple health check endpoint"""
    return web.Response(text="Bot is running", status=200)

async def bot_status(request):
    """Status endpoint for the bot"""
    stats = excel_manager.get_stats()
    status = {
        'status': 'Running',
        'stat_channel': detected_stat_channel,
        'display_channel': detected_display_channel,
        'excel_predictions': stats
    }
    return web.json_response(status)

async def create_web_server():
    """Create and start the aiohttp web server"""
    app = web.Application()
    app.router.add_get('/', health_check)
    app.router.add_get('/health', health_check)
    app.router.add_get('/status', bot_status)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()
    print(f"✅ Serveur web démarré sur 0.0.0.0:{PORT}")
    return runner

# --- LANCEMENT PRINCIPAL ---
async def main():
    """Fonction principale pour démarrer le bot"""
    print("Démarrage du bot Telegram...")

    if not API_ID or not API_HASH or not BOT_TOKEN:
        print("❌ Configuration manquante! Veuillez vérifier votre fichier .env")
        return

    try:
        # Démarrage du serveur web
        web_runner = await create_web_server()

        # Démarrage du bot
        if await start_bot():
            print("✅ Bot en ligne et en attente de messages...")
            print(f"🌐 Accès web: http://0.0.0.0:{PORT}")

            # Démarrage du surveillant de fichiers Excel en arrière-plan
            excel_watcher_task = asyncio.create_task(excel_file_watcher())

            await client.run_until_disconnected()

            # Annuler le watcher quand le bot s'arrête
            excel_watcher_task.cancel()
        else:
            print("❌ Échec du démarrage du bot")

    except KeyboardInterrupt:
        print("\n🛑 Arrêt du bot demandé par l'utilisateur")
    except Exception as e:
        print(f"❌ Erreur critique: {e}")

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Arrêt du script.")
    except Exception as e:
        print(f"Erreur fatale à l'exécution: {e}")