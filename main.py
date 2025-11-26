import os
import asyncio
import re
import json
import zipfile
import tempfile
import shutil
from datetime import datetime
from telethon import TelegramClient, events
from telethon.events import ChatAction
from dotenv import load_dotenv
from predictor import CardPredictor
from yaml_manager import init_database, db # Conservé pour la compatibilité
from excel_importer import ExcelPredictionManager
from aiohttp import web
import threading
from typing import Optional, Dict, Any, List

# Load environment variables
load_dotenv()

# --- CONFIGURATION ---
try:
    API_ID = int(os.getenv('API_ID') or '0')
    API_HASH = os.getenv('API_HASH') or ''
    BOT_TOKEN = os.getenv('BOT_TOKEN') or ''
    ADMIN_ID = int(os.getenv('ADMIN_ID') or '0') if os.getenv('ADMIN_ID') else None
    PORT = int(os.getenv('PORT') or '5000')
    DISPLAY_CHANNEL = int(os.getenv('DISPLAY_CHANNEL') or '-1002999811353') # ID par défaut ou fallback

    if not API_ID or API_ID == 0 or not API_HASH or not BOT_TOKEN:
        raise ValueError("Variables d'environnement API_ID, API_HASH, ou BOT_TOKEN manquantes.")

    print(f"✅ Configuration chargée: API_ID={API_ID}, ADMIN_ID={ADMIN_ID or 'Non configuré'}, PORT={PORT}")
except Exception as e:
    print(f"❌ Erreur configuration: {e}")
    exit(1)

# Fichier de configuration persistante
CONFIG_FILE = 'bot_config (1).json'

# Variables d'état
detected_stat_channel: Optional[int] = None
detected_display_channel: Optional[int] = None
prediction_interval = 1 # Intervalle par défaut

# Initialisation des gestionnaires
database = init_database()
predictor = CardPredictor()
excel_manager = ExcelPredictionManager()

# Variables pour l'état du bot
confirmation_pending = {} 

def load_config():
    """Load configuration from JSON file (source de vérité)"""
    global detected_stat_channel, detected_display_channel, prediction_interval
    try:
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                config = json.load(f)
                detected_stat_channel = config.get('stat_channel')
                detected_display_channel = config.get('display_channel', DISPLAY_CHANNEL)
                prediction_interval = config.get('prediction_interval', 1)
                print(f"✅ Configuration chargée depuis JSON: Stats={detected_stat_channel}, Display={detected_display_channel}, Intervalle={prediction_interval}min")
                return
    except Exception as e:
        print(f"⚠️ Erreur chargement configuration JSON: {e}")
        # Fallback si le fichier JSON est corrompu ou incomplet
        detected_display_channel = DISPLAY_CHANNEL
        prediction_interval = 1

def save_config():
    """Save configuration to JSON file"""
    try:
        config = {
            'stat_channel': detected_stat_channel,
            'display_channel': detected_display_channel,
            'prediction_interval': prediction_interval
        }
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(config, f, indent=2)
        print(f"💾 Configuration sauvegardée: Stats={detected_stat_channel}, Display={detected_display_channel}")
    except Exception as e:
        print(f"❌ Erreur sauvegarde configuration: {e}")

# Initialisation du client (chargement des configs)
load_config()

# Déclaration du client après chargement des configs pour utiliser les globals
session_name = f'bot_session_{int(datetime.now().timestamp())}'
client = TelegramClient(session_name, API_ID, API_HASH)

async def start_bot():
    """Start the bot and connect"""
    try:
        await client.start(bot_token=BOT_TOKEN)
        me = await client.get_me()
        print(f"Bot connecté: @{getattr(me, 'username', 'Unknown')}")
        return True
    except Exception as e:
        print(f"Erreur lors du démarrage du bot: {e}")
        return False

async def update_prediction_status(pred: dict, numero: int, winner: str, status: str, verified: bool):
    """Mise à jour unifiée du statut de prédiction et édition du message"""
    msg_id = pred.get("message_id")
    channel_id = pred.get("channel_id")

    if msg_id and channel_id:
        # Utiliser la nouvelle fonction pour obtenir le format complet avec le placeholder :⏳
        full_base_text_with_placeholder = excel_manager.get_prediction_format(numero, winner)
        
        # Le format complet est: 🔵{numero}:🅿️+6,5🔵statut :⏳
        # Nous remplaçons la fin :⏳ par le nouveau statut
        
        # Sépare le texte avant 'statut :⏳'
        base_format = full_base_text_with_placeholder.rsplit("statut :⏳", 1)[0]
        
        # Reconstruit le message avec le nouveau statut
        new_text = f"{base_format}statut :{status}" 

        try:
            await client.edit_message(channel_id, msg_id, new_text)
            pred["verified"] = verified
            excel_manager.save_predictions()
            print(f"✅ Prédiction #{numero} mise à jour: {status}")
        except Exception as e:
            print(f"❌ Erreur mise à jour message #{numero}: {e}")

async def verify_excel_predictions(game_number: int, message_text: str):
    """Fonction consolidée pour vérifier toutes les prédictions Excel en attente"""
    for key, pred in list(excel_manager.predictions.items()):
        if not pred["launched"] or pred.get("verified", False):
            continue

        pred_numero = pred["numero"]
        expected_winner = pred["victoire"]
        current_offset = pred.get("current_offset", 0)
        target_number = pred_numero + current_offset

        # Gestion du saut de numéro (si le bot était hors ligne ou retardé)
        if game_number > target_number and current_offset < 2:
            print(f"⚠️ Numéro sauté: #{pred_numero} attendait #{target_number}, reçu #{game_number}")
            current_offset = game_number - pred_numero
            if current_offset > 2:
                # Échec définitif si le saut dépasse l'offset maximum
                await update_prediction_status(pred, pred_numero, expected_winner, "❌", True)
                continue
            
            pred["current_offset"] = current_offset
            excel_manager.save_predictions()
            print(f"⏭️ Prédiction #{pred_numero}: saut direct à offset {current_offset}")


        # Vérification séquentielle
        status, should_continue = excel_manager.verify_excel_prediction(
            game_number, message_text, pred_numero, expected_winner, current_offset
        )

        if status:
            await update_prediction_status(pred, pred_numero, expected_winner, status, True)
        elif should_continue and game_number == pred_numero + current_offset:
            # Si on doit continuer et que c'est le numéro cible, incrémenter l'offset
            new_offset = current_offset + 1
            if new_offset <= 2:
                pred["current_offset"] = new_offset
                excel_manager.save_predictions()
                print(f"⏭️ Prédiction #{pred_numero}: nouvel offset {new_offset}")
            else:
                # Échec définitif après offset 2 non réussi (géré par verify_excel_prediction aussi, mais sécurisé ici)
                await update_prediction_status(pred, pred_numero, expected_winner, "❌", True)


# Fin de la partie 1/3
# --- INVITATION / CONFIRMATION ---
@client.on(events.ChatAction())
async def handler_join(event):
    """Handle bot joining channels/groups and send private invitation to admin"""
    global confirmation_pending

    try:
        if event.user_joined or event.user_added:
            me = await client.get_me()
            me_id = getattr(me, 'id', None)

            if event.user_id == me_id:
                confirmation_pending[event.chat_id] = 'waiting_confirmation'
                try:
                    chat = await client.get_entity(event.chat_id)
                    chat_title = getattr(chat, 'title', f'Canal {event.chat_id}')
                except:
                    chat_title = f'Canal {event.chat_id}'

                invitation_msg = f"""🔔 **Nouveau canal détecté**
📋 **Canal** : {chat_title}
🆔 **ID** : {event.chat_id}
**Choisissez le type de canal** :
• `/force_set_stat {event.chat_id}` - Canal de statistiques
• `/force_set_display {event.chat_id}` - Canal de diffusion"""

                if ADMIN_ID:
                    await client.send_message(ADMIN_ID, invitation_msg)
                print(f"Invitation envoyée à l'admin pour le canal: {chat_title} ({event.chat_id})")
    except Exception as e:
        print(f"Erreur dans handler_join: {e}")

# --- COMMANDES DE CONFIGURATION (Admin uniquement) ---

@client.on(events.NewMessage(pattern=r'/force_set_stat (-?\d+)'))
async def force_set_stat_channel(event):
    """Force set statistics channel (admin only)"""
    global detected_stat_channel

    if ADMIN_ID and event.sender_id != ADMIN_ID: return
    if event.is_group or event.is_channel: return

    channel_id = int(event.pattern_match.group(1))
    detected_stat_channel = channel_id
    save_config()

    try:
        chat = await client.get_entity(channel_id)
        chat_title = getattr(chat, 'title', f'Canal {channel_id}')
        await event.respond(f"✅ **Canal de statistiques configuré (force)**\n📋 {chat_title}\n🆔 ID: {channel_id}")
    except:
        await event.respond(f"✅ **Canal de statistiques configuré (force)**\n🆔 ID: {channel_id} (Titre non récupéré)")


@client.on(events.NewMessage(pattern=r'/force_set_display (-?\d+)'))
async def force_set_display_channel(event):
    """Force set display channel (admin only)"""
    global detected_display_channel

    if ADMIN_ID and event.sender_id != ADMIN_ID: return
    if event.is_group or event.is_channel: return

    channel_id = int(event.pattern_match.group(1))
    detected_display_channel = channel_id
    save_config()

    try:
        chat = await client.get_entity(channel_id)
        chat_title = getattr(chat, 'title', f'Canal {channel_id}')
        await event.respond(f"✅ **Canal de diffusion configuré (force)**\n📋 {chat_title}\n🆔 ID: {channel_id}")
    except:
        await event.respond(f"✅ **Canal de diffusion configuré (force)**\n🆔 ID: {channel_id} (Titre non récupéré)")

# --- COMMANDES UTILITAIRES ---

@client.on(events.NewMessage(pattern='/start'))
async def start_command(event):
    """Send welcome message"""
    if ADMIN_ID and event.sender_id != ADMIN_ID: return

    welcome_msg = """🎯 **Bot de Prédiction de Cartes - Bienvenue !**
🔹 **Développé par Sossou Kouamé Appolinaire**

**Format de prédiction** :
• Joueur (P+6,5) : 🔵XXX:🅿️+6,5🔵statut :⏳
• Banquier (M-4,5) : 🔵XXX:Ⓜ️-4,,5🔵statut :⏳

**Commandes** :
• `/start` : Ce message
• `/status` : État du bot et des canaux
• `/stats` : Statistiques de performance et Excel
• `/clear_excel` : Effacer toutes les prédictions Excel
• **Importation Excel** : Envoyez un fichier `.xlsx` au bot en privé.
"""
    await event.respond(welcome_msg)

@client.on(events.NewMessage(pattern='/status'))
async def show_status(event):
    """Show bot status (admin only)"""
    if ADMIN_ID and event.sender_id != ADMIN_ID: return
    load_config()

    status_msg = f"""📊 **Statut du Bot**
Canal statistiques: {'✅ Configuré' if detected_stat_channel else '❌ Non configuré'} ({detected_stat_channel})
Canal diffusion: {'✅ Configuré' if detected_display_channel else '❌ Non configuré'} ({detected_display_channel})
⏱️ Intervalle de prédiction: {prediction_interval} minutes
Prédictions actives (Excel): {excel_manager.get_stats()['launched']}
"""
    await event.respond(status_msg)

@client.on(events.NewMessage(pattern='/stats'))
async def show_excel_stats(event):
    """Show Excel predictions statistics (admin only)"""
    if ADMIN_ID and event.sender_id != ADMIN_ID: return
    stats = excel_manager.get_stats()

    msg = f"""📊 **Statut des Prédictions Excel**
• Total prédictions: {stats['total']}
• En attente: {stats['pending']}
• Lancées (En cours ou terminées): {stats['launched']}
"""
    await event.respond(msg)


@client.on(events.NewMessage(pattern='/clear_excel'))
async def clear_excel_handler(event):
    """Efface toutes les prédictions Excel (admin uniquement)"""
    if ADMIN_ID and event.sender_id != ADMIN_ID: return
    
    excel_manager.clear_predictions()
    await event.respond("🗑️ **Toutes les prédictions Excel** ont été effacées. Le bot est prêt pour un nouvel import.")

@client.on(events.NewMessage(pattern='/reset'))
async def reset_data(event):
    """Réinitialisation des données (admin uniquement)"""
    if ADMIN_ID and event.sender_id != ADMIN_ID: return

    predictor.reset()
    excel_manager.clear_predictions()

    msg = """🔄 **Données réinitialisées avec succès !**
✅ Prédictions Excel: vidées
✅ Base de données historique (predictor): réinitialisée
"""
    await event.respond(msg)

@client.on(events.NewMessage(pattern=r'/upload_excel', func=lambda e: e.is_private and e.sender_id == ADMIN_ID and e.media))
async def handle_excel_upload(event):
    """Handle Excel file upload from admin in private chat"""
    try:
        if not event.message.file or not any(mime in event.message.file.mime_type for mime in ['application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', 'application/vnd.ms-excel']):
            return await event.respond("❌ **Erreur**: Veuillez envoyer un fichier au format `.xlsx` ou `.xls`.")

        await event.respond("📥 **Téléchargement du fichier Excel...**")
        
        with tempfile.TemporaryDirectory() as temp_dir:
            file_path = os.path.join(temp_dir, event.file.name)
            await event.message.download_media(file=file_path)

            await event.respond("⚙️ **Importation des prédictions...**")
            result = excel_manager.import_excel(file_path, replace_mode=True)

        if result["success"]:
            stats = excel_manager.get_stats()
            consecutive_info = f"\n• Numéros consécutifs ignorés: {result.get('consecutive_skipped', 0)}" if result.get('consecutive_skipped', 0) > 0 else ""
            
            msg = f"""✅ **Import Excel réussi!**
📊 **Résumé**:
• Prédictions importées: {result['imported']}
• Total en base: {stats['total']}{consecutive_info}

📋 **Statistiques**:
• En attente: {stats['pending']}
• Lancées: {stats['launched']}

⚠️ **Note**: Le bot va surveiller le canal source et lancer les prédictions automatiquement."""
            await event.respond(msg)
        else:
            await event.respond(f"❌ **Erreur importation Excel**: {result['error']}")

    except Exception as e:
        print(f"Erreur dans handle_excel_upload: {e}")
        await event.respond(f"❌ **Erreur critique lors de l'import**: {e}")

# Fin de la partie 2/3
# --- LOGIQUE PRINCIPALE : ÉCOUTE DU CANAL SOURCE ---

@client.on(events.NewMessage(chats=detected_stat_channel))
async def handle_new_message(event):
    """Gère les nouveaux messages dans le canal de statistiques (source)"""
    if event.is_channel and event.chat_id == detected_stat_channel:
        message_text = event.raw_text
        game_number = predictor.extract_game_number(message_text)

        if game_number:
            # --- ÉTAPE 1: LANCEMENT DE LA PRÉDICTION EXCEL ---
            if detected_display_channel and detected_stat_channel:
                # Trouver la prédiction la plus proche (dans la tolérance)
                closest_pred = excel_manager.find_close_prediction(game_number)

                if closest_pred:
                    pred_key = closest_pred["key"]
                    pred_numero = closest_pred["prediction"]["numero"]
                    pred_victoire = closest_pred["prediction"]["victoire"]

                    # Création du message avec le nouveau format (ex: 🔵XXX:🅿️+6,5🔵statut :⏳)
                    prediction_text = excel_manager.get_prediction_format(pred_numero, pred_victoire)

                    try:
                        sent_message = await client.send_message(detected_display_channel, prediction_text)
                        # Marquer comme lancé et enregistrer l'ID du message
                        excel_manager.mark_as_launched(pred_key, sent_message.id, detected_display_channel)
                        ecart = pred_numero - game_number
                        print(f"✅ Prédiction Excel lancée: {prediction_text} | Canal source: #{game_number} (écart: +{ecart} parties)")
                    except Exception as e:
                        print(f"❌ Erreur envoi prédiction Excel: {e}")

            # --- ÉTAPE 2: VÉRIFICATION DES PRÉDICTIONS EXCEL LANCÉES ---
            await verify_excel_predictions(game_number, message_text)
            
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
            await client.run_until_disconnected()
        else:
            print("❌ Échec du démarrage du bot")

    except KeyboardInterrupt:
        print("\n🛑 Arrêt du bot demandé par l'utilisateur")
    except Exception as e:
        print(f"❌ Erreur critique: {e}")
        
if __name__ == '__main__':
    try:
        # L'utilisation de client.loop.run_until_complete(main()) est dépréciée
        asyncio.run(main()) 
    except KeyboardInterrupt:
        print("Arrêt du script.")
    except Exception as e:
        print(f"Erreur fatale à l'exécution: {e}")
                        
