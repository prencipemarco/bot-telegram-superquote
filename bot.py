import logging
import re
import os
from datetime import datetime
from typing import Dict, List
from telegram import Update
from telegram.ext import Application, MessageHandler, CommandHandler, ContextTypes, filters
import pandas as pd
from pymongo import MongoClient
from pymongo.errors import ConnectionFailure

# Configurazione logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO
)
logger = logging.getLogger(__name__)

class SuperquoteBot:
    def __init__(self, token: str):
        self.token = token
        self.mongo_uri = os.getenv('MONGO_URI', 'mongodb://localhost:27017')
        self.db_name = os.getenv('DB_NAME', 'superquote_bot')
        self.collection_name = 'superquotes'
        self.client = None
        self.db = None
        self.collection = None
        self._connect_to_mongo()
        
    def _connect_to_mongo(self):
        """Connessione a MongoDB con gestione errori"""
        try:
            self.client = MongoClient(self.mongo_uri)
            # Test della connessione
            self.client.admin.command('ping')
            self.db = self.client[self.db_name]
            self.collection = self.db[self.collection_name]
            logger.info("✅ Connesso a MongoDB con successo!")
            
            # Crea indici per le query
            self.collection.create_index([("data", -1)])
            self.collection.create_index([("user_id", 1)])
            self.collection.create_index([("esito", 1)])
            
        except ConnectionFailure as e:
            logger.error(f"❌ Errore connessione MongoDB: {e}")
            raise
    
    def load_data(self) -> List[Dict]:
        """Carica tutti i dati da MongoDB"""
        try:
            cursor = self.collection.find({}).sort('data', -1)
            data = list(cursor)
            
            # Converti ObjectId to string per compatibilità
            for item in data:
                item['_id'] = str(item['_id'])
            
            return data
        except Exception as e:
            logger.error(f"Errore nel caricamento dati: {e}")
            return []
    
    def save_superquote(self, superquote: Dict) -> bool:
        """Salva una superquote in MongoDB"""
        try:
            # Crea una copia per non modificare l'originale
            sq_copy = superquote.copy()
            # Rimuovi _id se presente per evitare duplicati
            sq_copy.pop('_id', None)
            
            result = self.collection.insert_one(sq_copy)
            logger.info(f"Superquote salvata con ID: {result.inserted_id}")
            return True
        except Exception as e:
            logger.error(f"Errore nel salvataggio: {e}")
            return False
    
    def get_all_superquotes(self) -> List[Dict]:
        """Ottiene tutte le superquote ordinate per data (più recenti prima)"""
        return self.load_data()
    
    def get_wins(self) -> List[Dict]:
        """Ottiene solo le superquote vinte"""
        try:
            cursor = self.collection.find({"esito": "VINTA"}).sort('data', -1)
            wins = list(cursor)
            for item in wins:
                item['_id'] = str(item['_id'])
            return wins
        except Exception as e:
            logger.error(f"Errore nel recupero vincite: {e}")
            return []
    
    def get_stats_data(self) -> pd.DataFrame:
        """Ottiene i dati per le statistiche"""
        data = self.load_data()
        return pd.DataFrame(data) if data else pd.DataFrame()

    def parse_superquote(self, text: str) -> Dict or None:
        """
        Parsing del messaggio superquote
        Formato: SQ-risultato-quota-vincita-esito
        Esempio: SQ-1MILAN-2.00-20.00-VINTA
        """
        # Rimuovi spazi e converti in maiuscolo per il controllo
        text_clean = text.strip()
        
        # Pattern regex più flessibile per catturare il formato SQ-risultato-quota-vincita-esito
        pattern = r'^SQ-([^-]+)-([0-9]+(?:\.[0-9]+)?)-([0-9]+(?:\.[0-9]+)?)-([^-]+)$'
        match = re.match(pattern, text_clean, re.IGNORECASE)
        
        if match:
            risultato = match.group(1).strip()
            try:
                quota = float(match.group(2))
                vincita = float(match.group(3))
            except ValueError:
                return None
            
            esito = match.group(4).strip().upper()
            
            # Normalizza l'esito
            if esito in ['VINTA', 'VINCITA', 'WIN', 'W']:
                esito = 'VINTA'
            elif esito in ['PERSA', 'PERDITA', 'LOSS', 'L', 'PERSO']:
                esito = 'PERSA'
            else:
                return None  # Esito non valido
            
            return {
                'risultato': risultato,
                'quota': quota,
                'vincita': vincita,
                'esito': esito,
                'data': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'messaggio_originale': text_clean
            }
        return None
    
    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Gestisce i messaggi in arrivo"""
        if update.message and update.message.text:
            message_text = update.message.text.strip()
            username = update.message.from_user.username or update.message.from_user.first_name or "Anonimo"
            
            # Verifica se il messaggio inizia con "SQ"
            if message_text.upper().startswith('SQ'):
                superquote = self.parse_superquote(message_text)
                
                if superquote:
                    # Aggiungi informazioni utente per riferimento interno
                    superquote['registrato_da'] = username
                    superquote['user_id'] = update.message.from_user.id
                    
                    # Salva la superquote in MongoDB
                    success = self.save_superquote(superquote)
                    
                    if success:
                        # Conferma ricezione
                        await update.message.reply_text(
                            f"✅ Superquote registrata!\n"
                            f"🎯 Risultato: {superquote['risultato']}\n"
                            f"💰 Quota: {superquote['quota']}\n"
                            f"💵 Vincita: €{superquote['vincita']:.2f}\n"
                            f"📊 Esito: {superquote['esito']}\n"
                            f"📅 Data: {superquote['data'][:16]}"
                        )
                    else:
                        await update.message.reply_text("❌ Errore nel salvataggio dei dati!")
                else:
                    await update.message.reply_text(
                        "❌ Formato non valido!\n\n"
                        "📝 Usa il formato: SQ-risultato-quota-vincita-esito\n\n"
                        "🎯 Esempi corretti:\n"
                        "• SQ-1MILAN-2.00-20.00-VINTA\n"
                        "• SQ-OVER2.5-1.85-0.00-PERSA\n"
                        "• SQ-COMBO-3.20-160.00-VINTA"
                    )
    
    async def show_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Mostra le statistiche delle superquote"""
        df = self.get_stats_data()
        
        if df.empty:
            await update.message.reply_text("📊 Nessuna superquote registrata ancora!")
            return
        
        stats_text = "📊 **STATISTICHE SUPERQUOTE CONDIVISE**\n\n"
        
        # Statistiche generali
        total_superquote = len(df)
        vinte = len(df[df['esito'] == 'VINTA'])
        perse = len(df[df['esito'] == 'PERSA'])
        
        stats_text += f"🎯 Totale superquote: {total_superquote}\n"
        stats_text += f"✅ Vinte: {vinte}\n"
        stats_text += f"❌ Perse: {perse}\n"
        
        if total_superquote > 0:
            percentuale_successo = (vinte / total_superquote) * 100
            stats_text += f"📈 % Successo: {percentuale_successo:.1f}%\n"
        
        # Statistiche economiche
        vincita_totale = df['vincita'].sum()
        vincita_media = df['vincita'].mean()
        quota_media = df['quota'].mean()
        
        stats_text += f"\n💰 **DATI ECONOMICI:**\n"
        stats_text += f"💵 Vincita totale: €{vincita_totale:.2f}\n"
        stats_text += f"📊 Vincita media: €{vincita_media:.2f}\n"
        stats_text += f"🎲 Quota media: {quota_media:.2f}\n"
        
        # Migliori risultati
        if len(df) > 0:
            # Miglior vincita
            best_win = df.loc[df['vincita'].idxmax()]
            stats_text += f"\n🏆 **MIGLIOR VINCITA:**\n"
            stats_text += f"🎯 {best_win['risultato']}\n"
            stats_text += f"💰 Quota {best_win['quota']} → €{best_win['vincita']:.2f}\n"
            stats_text += f"📅 {best_win['data'][:10]}\n"
            
            # Quota più alta vinta
            won_bets = df[df['esito'] == 'VINTA']
            if len(won_bets) > 0:
                highest_won_odds = won_bets.loc[won_bets['quota'].idxmax()]
                stats_text += f"\n🎰 **QUOTA PIÙ ALTA VINTA:**\n"
                stats_text += f"🎯 {highest_won_odds['risultato']}\n"
                stats_text += f"💰 Quota {highest_won_odds['quota']}\n"
        
        await update.message.reply_text(stats_text, parse_mode='Markdown')
    
    async def show_list(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Mostra la lista delle superquote recenti"""
        superquotes = self.get_all_superquotes()
        
        if not superquotes:
            await update.message.reply_text("📝 Nessuna superquote registrata ancora!")
            return
        
        list_text = "📝 **ULTIME SUPERQUOTE**\n\n"
        
        # Mostra le ultime 12 per non superare il limite messaggi
        for sq in superquotes[:12]:
            icon = "✅" if sq['esito'] == 'VINTA' else "❌"
            data_breve = sq['data'][:10]  # Solo la data, senza ora
            
            list_text += f"{icon} **{sq['risultato']}**\n"
            list_text += f"    💰 {sq['quota']} → €{sq['vincita']:.2f} | {data_breve}\n\n"
        
        if len(superquotes) > 12:
            list_text += f"📋 ... e altre {len(superquotes) - 12} superquote\n"
            list_text += "Usa /export per il file completo"
        
        await update.message.reply_text(list_text, parse_mode='Markdown')
    
    async def show_recent_wins(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Mostra solo le vincite recenti"""
        wins = self.get_wins()
        
        if not wins:
            await update.message.reply_text("🎯 Nessuna vincita registrata ancora!")
            return
        
        list_text = "🏆 **ULTIME VINCITE**\n\n"
        
        for sq in wins[:10]:  # Ultime 10 vincite
            data_breve = sq['data'][:10]
            list_text += f"✅ **{sq['risultato']}**\n"
            list_text += f"    💰 {sq['quota']} → €{sq['vincita']:.2f} | {data_breve}\n\n"
        
        if len(wins) > 10:
            list_text += f"🎯 Totale vincite: {len(wins)}"
        
        await update.message.reply_text(list_text, parse_mode='Markdown')
    
    async def export_csv(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Esporta i dati in formato CSV"""
        superquotes = self.get_all_superquotes()
        
        if not superquotes:
            await update.message.reply_text("📊 Nessun dato da esportare!")
            return
        
        try:
            # Crea DataFrame e riordina le colonne
            df = pd.DataFrame(superquotes)
            
            # Riordina le colonne per leggibilità
            column_order = ['data', 'risultato', 'quota', 'vincita', 'esito', 'registrato_da']
            df = df.reindex(columns=[col for col in column_order if col in df.columns])
            
            # Nome file con timestamp
            csv_filename = f'superquote_export_{datetime.now().strftime("%Y%m%d_%H%M%S")}.csv'
            df.to_csv(csv_filename, index=False, encoding='utf-8')
            
            # Invia il file
            with open(csv_filename, 'rb') as csv_file:
                await update.message.reply_document(
                    document=csv_file,
                    filename=csv_filename,
                    caption=f"📊 Export completo delle superquote\n🎯 {len(df)} record esportati"
                )
            
            # Rimuovi il file temporaneo
            os.remove(csv_filename)
            
        except Exception as e:
            logger.error(f"Errore durante l'export: {e}")
            await update.message.reply_text("❌ Errore durante l'export. Riprova più tardi.")
    
    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Mostra i comandi disponibili"""
        help_text = """
🤖 **BOT SUPERQUOTE CONDIVISE**

📝 **COME REGISTRARE:**
Scrivi: `SQ-risultato-quota-vincita-esito`

🎯 **ESEMPI:**
• `SQ-1MILAN-2.00-20.00-VINTA`
• `SQ-OVER2.5-1.85-0.00-PERSA`
• `SQ-COMBO-3.20-160.00-VINTA`
• `SQ-GG-1.65-32.50-VINTA`

📊 **COMANDI:**
/stats - Statistiche complete
/lista - Ultime superquote
/vincite - Solo le vincite recenti  
/export - Esporta tutto in CSV
/help - Questo messaggio

🎲 **ESITI VALIDI:**
VINTA, VINCITA, WIN → registra come vincita
PERSA, PERDITA, LOSS → registra come perdita

Il bot salva automaticamente tutto in MongoDB! 🗂️
        """
        await update.message.reply_text(help_text, parse_mode='Markdown')
    
    def run(self):
        """Avvia il bot"""
        try:
            # Crea l'applicazione
            application = Application.builder().token(self.token).build()
            
            # Aggiungi handlers
            application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_message))
            application.add_handler(CommandHandler("stats", self.show_stats))
            application.add_handler(CommandHandler("lista", self.show_list))
            application.add_handler(CommandHandler("vincite", self.show_recent_wins))
            application.add_handler(CommandHandler("export", self.export_csv))
            application.add_handler(CommandHandler("help", self.help_command))
            application.add_handler(CommandHandler("start", self.help_command))
            
            # Avvia il bot
            logger.info("🤖 Bot Superquote avviato con successo!")
            print("🤖 Bot Superquote avviato! Premi Ctrl+C per fermare.")
            application.run_polling()
            
        except Exception as e:
            logger.error(f"Errore nell'avvio del bot: {e}")
            print(f"❌ Errore: {e}")

if __name__ == '__main__':
    import os
    
    # Leggi le variabili ambiente
    BOT_TOKEN = os.getenv('BOT_TOKEN')
    MONGO_URI = os.getenv('MONGO_URI')
    
    if not BOT_TOKEN:
        print("❌ ERRORE: Variabile BOT_TOKEN non trovata!")
        print("\n📱 STEPS:")
        print("1. Crea il bot con @BotFather su Telegram")
        print("2. Copia il token")
        print("3. Su Railway, vai in Variables → Add BOT_TOKEN")
        exit(1)
    
    if not MONGO_URI:
        print("⚠️  MONGO_URI non trovato, uso default locale")
        print("💡 Su Railway, aggiungi MONGO_URI in Variables")
    
    # Verifica dipendenze
    try:
        import pandas as pd
        from pymongo import MongoClient
    except ImportError as e:
        print(f"❌ ERRORE: Dipendenze mancanti - {e}")
        print("📦 Installa con: pip install -r requirements.txt")
        exit(1)
    
    # Avvia il bot
    bot = SuperquoteBot(BOT_TOKEN)
    bot.run()