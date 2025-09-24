import logging
import re
import json
import os
from datetime import datetime
from typing import Dict, List
from telegram import Update
from telegram.ext import Application, MessageHandler, CommandHandler, ContextTypes, filters
import pandas as pd

# Configurazione logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO
)
logger = logging.getLogger(__name__)

class SuperquoteBot:
    def __init__(self, token: str):
        self.token = token
        self.data_file = 'superquote_data.json'
        self.superquote_data = self.load_data()
        
    def load_data(self) -> List[Dict]:
        """Carica i dati dal file JSON"""
        if os.path.exists(self.data_file):
            try:
                with open(self.data_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except (json.JSONDecodeError, FileNotFoundError):
                return []
        return []
    
    def save_data(self):
        """Salva i dati nel file JSON"""
        with open(self.data_file, 'w', encoding='utf-8') as f:
            json.dump(self.superquote_data, f, ensure_ascii=False, indent=2)
    
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
                    
                    # Salva la superquote
                    self.superquote_data.append(superquote)
                    self.save_data()
                    
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
        if not self.superquote_data:
            await update.message.reply_text("📊 Nessuna superquote registrata ancora!")
            return
        
        # Calcola statistiche
        df = pd.DataFrame(self.superquote_data)
        
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
        if not self.superquote_data:
            await update.message.reply_text("📝 Nessuna superquote registrata ancora!")
            return
        
        # Ordina per data (più recenti prima)
        sorted_data = sorted(self.superquote_data, key=lambda x: x['data'], reverse=True)
        
        list_text = "📝 **ULTIME SUPERQUOTE**\n\n"
        
        # Mostra le ultime 12 per non superare il limite messaggi
        for sq in sorted_data[:12]:
            icon = "✅" if sq['esito'] == 'VINTA' else "❌"
            data_breve = sq['data'][:10]  # Solo la data, senza ora
            
            list_text += f"{icon} **{sq['risultato']}**\n"
            list_text += f"    💰 {sq['quota']} → €{sq['vincita']:.2f} | {data_breve}\n\n"
        
        if len(sorted_data) > 12:
            list_text += f"📋 ... e altre {len(sorted_data) - 12} superquote\n"
            list_text += "Usa /export per il file completo"
        
        await update.message.reply_text(list_text, parse_mode='Markdown')
    
    async def show_recent_wins(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Mostra solo le vincite recenti"""
        if not self.superquote_data:
            await update.message.reply_text("🎯 Nessuna superquote registrata ancora!")
            return
        
        # Filtra solo le vincite e ordina per data
        wins = [sq for sq in self.superquote_data if sq['esito'] == 'VINTA']
        wins_sorted = sorted(wins, key=lambda x: x['data'], reverse=True)
        
        if not wins_sorted:
            await update.message.reply_text("🎯 Nessuna vincita registrata ancora!")
            return
        
        list_text = "🏆 **ULTIME VINCITE**\n\n"
        
        for sq in wins_sorted[:10]:  # Ultime 10 vincite
            data_breve = sq['data'][:10]
            list_text += f"✅ **{sq['risultato']}**\n"
            list_text += f"    💰 {sq['quota']} → €{sq['vincita']:.2f} | {data_breve}\n\n"
        
        if len(wins_sorted) > 10:
            list_text += f"🎯 Totale vincite: {len(wins_sorted)}"
        
        await update.message.reply_text(list_text, parse_mode='Markdown')
    
    async def export_csv(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Esporta i dati in formato CSV"""
        if not self.superquote_data:
            await update.message.reply_text("📊 Nessun dato da esportare!")
            return
        
        try:
            # Crea DataFrame e riordina le colonne
            df = pd.DataFrame(self.superquote_data)
            
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

Il bot salva automaticamente tutto in un archivio condiviso! 🗂️
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
    
    # Leggi il token dalle variabili ambiente
    BOT_TOKEN = os.getenv('BOT_TOKEN')
    
    if not BOT_TOKEN:
        print("❌ ERRORE: Variabile BOT_TOKEN non trovata!")
        print("\n📱 STEPS:")
        print("1. Crea il bot con @BotFather su Telegram")
        print("2. Copia il token")
        print("3. Su Render, vai in Environment → Add BOT_TOKEN")
        exit(1)
    
    # Verifica pandas
    try:
        import pandas as pd
    except ImportError:
        print("❌ ERRORE: pandas non installato")
        exit(1)
    
    # Avvia il bot
    bot = SuperquoteBot(BOT_TOKEN)
    bot.run()