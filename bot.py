import logging
import re
import os
import csv
import io
import uuid
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from datetime import datetime
from typing import Dict, List, Optional
from telegram import Update
from telegram.ext import Application, MessageHandler, CommandHandler, ContextTypes, filters
from pymongo import MongoClient
from pymongo.errors import ConnectionFailure, ServerSelectionTimeoutError

# Configurazione logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO
)
logger = logging.getLogger(__name__)

class SuperquoteBot:
    def __init__(self, token: str, mongo_uri: str):
        self.token = token
        self.mongo_uri = mongo_uri
        self.db_name = 'superquote_bot'
        self.collection_name = 'superquotes'
        self.client = None
        self.db = None
        self.collection = None
        self._connect_to_mongo()
        
    def _connect_to_mongo(self):
        """Connessione a MongoDB con gestione errori migliorata"""
        try:
            logger.info(f"🔗 Tentativo connessione a MongoDB: {self.mongo_uri[:30]}...")
            
            # Configurazione MongoDB con parametri ottimizzati per Railway
            self.client = MongoClient(
                self.mongo_uri,
                serverSelectionTimeoutMS=10000,
                connectTimeoutMS=20000,
                socketTimeoutMS=20000,
                maxPoolSize=10,
                retryWrites=True,
                w='majority'
            )
            
            # Test della connessione
            self.client.admin.command('ping')
            
            self.db = self.client[self.db_name]
            self.collection = self.db[self.collection_name]
            
            logger.info("✅ Connesso a MongoDB con successo!")
            
            # Crea indici
            try:
                self.collection.create_index([("data", -1)])
                self.collection.create_index([("user_id", 1)])
                self.collection.create_index([("esito", 1)])
                self.collection.create_index([("quote_id", 1)])
                logger.info("📋 Indici database creati")
            except Exception as idx_error:
                logger.warning(f"⚠️ Errore creazione indici: {idx_error}")
            
        except Exception as e:
            logger.error(f"❌ Errore connessione MongoDB: {e}")
            raise ConnectionFailure(f"Impossibile connettersi a MongoDB: {e}")
    
    def generate_quote_id(self) -> str:
        """Genera un ID univoco per la giocata (8 caratteri)"""
        return str(uuid.uuid4())[:8].upper()
    
    def get_all_superquotes(self) -> List[Dict]:
        """Ottiene tutte le superquote ordinate per data (più recenti prima)"""
        try:
            cursor = self.collection.find({}).sort('data', -1).limit(1000)
            data = list(cursor)
            
            for item in data:
                item['_id'] = str(item['_id'])
            
            return data
        except Exception as e:
            logger.error(f"Errore nel caricamento dati: {e}")
            return []
    
    def find_superquote_by_id(self, quote_id: str) -> Optional[Dict]:
        """Trova una superquote tramite il suo ID univoco"""
        try:
            result = self.collection.find_one({"quote_id": quote_id.upper()})
            if result:
                result['_id'] = str(result['_id'])
            return result
        except Exception as e:
            logger.error(f"Errore nella ricerca per ID: {e}")
            return None
    
    def calculate_winning_amount(self, quota: float, importo: float, esito: str) -> float:
        """Calcola la vincita in base a quota, importo e esito"""
        if esito == "VINTA":
            return quota * importo
        else:
            return 0.0
    
    def calculate_balance(self) -> Dict:
        """Calcola il saldo totale (vincite - perdite)"""
        try:
            superquotes = self.get_all_superquotes()
            
            total_bet = sum(sq['importo'] for sq in superquotes)
            total_winnings = sum(sq['vincita'] for sq in superquotes if sq['esito'] == 'VINTA')
            total_losses = sum(sq['importo'] for sq in superquotes if sq['esito'] == 'PERSA')
            
            balance = total_winnings - total_bet
            
            wins = len([sq for sq in superquotes if sq['esito'] == 'VINTA'])
            losses = len([sq for sq in superquotes if sq['esito'] == 'PERSA'])
            
            return {
                'saldo': balance,
                'total_bet': total_bet,
                'total_winnings': total_winnings,
                'total_losses': total_losses,
                'wins': wins,
                'losses': losses,
                'total_bets': len(superquotes)
            }
        except Exception as e:
            logger.error(f"Errore calcolo saldo: {e}")
            return {
                'saldo': 0.0,
                'total_bet': 0.0,
                'total_winnings': 0.0,
                'total_losses': 0.0,
                'wins': 0,
                'losses': 0,
                'total_bets': 0
            }
    
    def save_superquote(self, superquote: Dict) -> bool:
        """Salva una superquote in MongoDB"""
        try:
            sq_copy = superquote.copy()
            sq_copy.pop('_id', None)
            
            result = self.collection.insert_one(sq_copy)
            logger.info(f"Superquote salvata con ID: {result.inserted_id}")
            return True
            
        except Exception as e:
            logger.error(f"Errore nel salvataggio: {e}")
            return False
    
    def update_superquote(self, quote_id: str, updates: Dict) -> bool:
        """Aggiorna una superquote esistente con nuovi dati"""
        try:
            existing = self.find_superquote_by_id(quote_id)
            if not existing:
                return False
            
            update_fields = {}
            
            if 'risultato' in updates:
                update_fields['risultato'] = updates['risultato']
            
            if 'quota' in updates:
                new_quota = float(updates['quota'])
                update_fields['quota'] = new_quota
            
            if 'importo' in updates:
                new_importo = float(updates['importo'])
                update_fields['importo'] = new_importo
            
            if 'esito' in updates:
                new_esito = updates['esito'].upper()
                if new_esito in ['VINTA', 'VINCITA', 'WIN', 'W']:
                    new_esito = 'VINTA'
                elif new_esito in ['PERSA', 'PERDITA', 'LOSS', 'L', 'PERSO']:
                    new_esito = 'PERSA'
                else:
                    return False
                update_fields['esito'] = new_esito
            
            if any(field in updates for field in ['quota', 'importo', 'esito']):
                final_quota = update_fields.get('quota', existing['quota'])
                final_importo = update_fields.get('importo', existing['importo'])
                final_esito = update_fields.get('esito', existing['esito'])
                
                update_fields['vincita'] = self.calculate_winning_amount(
                    final_quota, final_importo, final_esito
                )
            
            update_fields['data_modifica'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            
            result = self.collection.update_one(
                {"quote_id": quote_id.upper()},
                {"$set": update_fields}
            )
            
            return result.modified_count > 0
            
        except Exception as e:
            logger.error(f"Errore nell'aggiornamento: {e}")
            return False
    
    def parse_modify_command(self, text: str) -> Optional[Dict]:
        """
        Parsing del comando di modifica avanzato
        Formati supportati:
        - MODIFICA-ID-ESITO
        - MODIFICA-ID-RISULTATO-QUOTA-IMPORTO-ESITO
        - MODIFICA-ID-CAMPO=VALORE
        """
        text_clean = text.strip().upper()
        
        # Formato semplice: MODIFICA-ID-ESITO
        simple_pattern = r'^MODIFICA-([A-Z0-9]{8})-([^-]+)$'
        simple_match = re.match(simple_pattern, text_clean)
        
        if simple_match:
            quote_id = simple_match.group(1)
            esito = simple_match.group(2)
            
            if esito in ['VINTA', 'VINCITA', 'WIN', 'W']:
                esito = 'VINTA'
            elif esito in ['PERSA', 'PERDITA', 'LOSS', 'L', 'PERSO']:
                esito = 'PERSA'
            else:
                return None
            
            return {
                'quote_id': quote_id,
                'updates': {'esito': esito},
                'tipo': 'semplice'
            }
        
        # Formato completo: MODIFICA-ID-RISULTATO-QUOTA-IMPORTO-ESITO
        full_pattern = r'^MODIFICA-([A-Z0-9]{8})-([^-]+)-([0-9.]+)-([0-9.]+)-([^-]+)$'
        full_match = re.match(full_pattern, text_clean)
        
        if full_match:
            quote_id = full_match.group(1)
            risultato = full_match.group(2)
            quota = full_match.group(3)
            importo = full_match.group(4)
            esito = full_match.group(5)
            
            if esito in ['VINTA', 'VINCITA', 'WIN', 'W']:
                esito = 'VINTA'
            elif esito in ['PERSA', 'PERDITA', 'LOSS', 'L', 'PERSO']:
                esito = 'PERSA'
            else:
                return None
            
            return {
                'quote_id': quote_id,
                'updates': {
                    'risultato': risultato,
                    'quota': quota,
                    'importo': importo,
                    'esito': esito
                },
                'tipo': 'completo'
            }
        
        # Formato campo singolo: MODIFICA-ID-CAMPO=VALORE
        field_pattern = r'^MODIFICA-([A-Z0-9]{8})-(RISULTATO|QUOTA|IMPORTO|ESITO)=(.+)$'
        field_match = re.match(field_pattern, text_clean)
        
        if field_match:
            quote_id = field_match.group(1)
            campo = field_match.group(2).lower()
            valore = field_match.group(3)
            
            updates = {campo: valore}
            
            return {
                'quote_id': quote_id,
                'updates': updates,
                'tipo': 'campo_singolo'
            }
        
        return None
    
    def delete_superquote(self, quote_id: str) -> bool:
        """Elimina una superquote tramite il suo ID"""
        try:
            result = self.collection.delete_one({"quote_id": quote_id.upper()})
            if result.deleted_count > 0:
                logger.info(f"Superquote {quote_id} eliminata con successo")
                return True
            else:
                logger.warning(f"Nessuna superquote trovata con ID {quote_id}")
                return False
        except Exception as e:
            logger.error(f"Errore nell'eliminazione: {e}")
            return False

    def parse_delete_command(self, text: str) -> Optional[str]:
        """
        Parsing del comando di eliminazione
        Formato: ELIMINA-ID o DELETE-ID
        Esempio: ELIMINA-A1B2C3D4
        """
        text_clean = text.strip()
        
        pattern = r'^(?:ELIMINA|DELETE)-([A-Z0-9]{8})$'
        match = re.match(pattern, text_clean, re.IGNORECASE)
        
        if match:
            quote_id = match.group(1).upper()
            return quote_id
        return None
    
    def get_recent_activity(self, limit: int = 10) -> List[Dict]:
        """Ottiene le attività recenti con limit personalizzato"""
        try:
            cursor = self.collection.find({}).sort('data', -1).limit(limit)
            data = list(cursor)
            for item in data:
                item['_id'] = str(item['_id'])
            return data
        except Exception as e:
            logger.error(f"Errore nel caricamento attività recenti: {e}")
            return []
    
    async def generate_profit_graph(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Genera e invia il grafico dell'andamento delle vincite"""
        try:
            superquotes = self.get_all_superquotes()
            
            if not superquotes:
                await update.message.reply_text("📊 Nessuna superquote registrata ancora! Non posso generare il grafico.")
                return
            
            superquotes_sorted = sorted(superquotes, key=lambda x: x['data'])
            
            dates = []
            cumulative_profit = []
            current_balance = 0
            
            for sq in superquotes_sorted:
                try:
                    date_obj = datetime.strptime(sq['data'], '%Y-%m-%d %H:%M:%S')
                except ValueError:
                    try:
                        date_obj = datetime.strptime(sq['data'][:10], '%Y-%m-%d')
                    except:
                        date_obj = datetime.now()
                
                dates.append(date_obj)
                
                if sq['esito'] == 'VINTA':
                    current_balance += sq['vincita'] - sq['importo']
                else:
                    current_balance -= sq['importo']
                
                cumulative_profit.append(current_balance)
            
            plt.figure(figsize=(10, 6))
            
            line_color = 'green' if current_balance >= 0 else 'red'
            fill_color = 'lightgreen' if current_balance >= 0 else 'lightcoral'
            
            plt.plot(dates, cumulative_profit, color=line_color, linewidth=2.5, label='Saldo')
            plt.fill_between(dates, cumulative_profit, alpha=0.3, color=fill_color)
            
            plt.axhline(y=0, color='black', linestyle='-', alpha=0.3, linewidth=1)
            
            plt.gca().xaxis.set_major_formatter(mdates.DateFormatter('%d/%m'))
            plt.gca().xaxis.set_major_locator(mdates.WeekdayLocator(interval=1))
            plt.gcf().autofmt_xdate()
            
            plt.title('📈 Andamento delle Vincite Cumulative', fontsize=14, fontweight='bold')
            plt.xlabel('Data')
            plt.ylabel('Saldo (€)')
            plt.grid(True, alpha=0.3)
            plt.legend()
            
            final_balance_text = f"Saldo finale: €{current_balance:.2f}"
            plt.annotate(final_balance_text, 
                        xy=(1, 0), xycoords='axes fraction',
                        xytext=(-10, 10), textcoords='offset points',
                        ha='right', va='bottom',
                        bbox=dict(boxstyle='round,pad=0.5', fc='yellow', alpha=0.7),
                        fontsize=10)
            
            graph_buffer = io.BytesIO()
            plt.savefig(graph_buffer, format='png', dpi=100, bbox_inches='tight')
            graph_buffer.seek(0)
            plt.close()
            
            balance_data = self.calculate_balance()
            saldo = balance_data['saldo']
            saldo_text = "POSITIVO 🟢" if saldo >= 0 else "NEGATIVO 🔴"
            
            caption = (
                f"📊 **GRAFICO ANDAMENTO VINCITE**\n\n"
                f"💰 **Saldo attuale:** €{saldo:.2f} ({saldo_text})\n"
                f"🎯 Giocate totali: {balance_data['total_bets']}\n"
                f"✅ Vincite: {balance_data['wins']} | ❌ Perdite: {balance_data['losses']}\n"
                f"📈 % Successo: {(balance_data['wins']/balance_data['total_bets']*100):.1f}%\n\n"
                f"🔄 Il grafico mostra l'andamento giocata per giocata"
            )
            
            await update.message.reply_photo(
                photo=graph_buffer,
                caption=caption,
                parse_mode='Markdown'
            )
            
        except Exception as e:
            logger.error(f"Errore nella generazione del grafico: {e}")
            await update.message.reply_text("❌ Errore nella generazione del grafico. Riprova più tardi.")
    
    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Gestisce i messaggi in arrivo"""
        if update.message and update.message.text:
            message_text = update.message.text.strip()
            username = update.message.from_user.username or update.message.from_user.first_name or "Anonimo"
            
            # Gestione comando MODIFICA
            if message_text.upper().startswith('MODIFICA'):
                modify_data = self.parse_modify_command(message_text)
                
                if modify_data:
                    existing = self.find_superquote_by_id(modify_data['quote_id'])
                    
                    if not existing:
                        await update.message.reply_text(f"❌ ID {modify_data['quote_id']} non trovato!")
                        return
                    
                    success = self.update_superquote(
                        modify_data['quote_id'], 
                        modify_data['updates']
                    )
                    
                    if success:
                        updated = self.find_superquote_by_id(modify_data['quote_id'])
                        
                        response_text = (
                            f"✅ Giocata modificata con successo!\n\n"
                            f"🆔 ID: {updated['quote_id']}\n"
                            f"🎯 Risultato: {updated['risultato']}\n"
                            f"💰 Quota: {updated['quota']}\n"
                            f"💵 Importo: €{updated['importo']:.2f}\n"
                            f"🏆 Vincita: €{updated['vincita']:.2f}\n"
                            f"📊 Esito: {updated['esito']}\n"
                            f"📅 Ultima modifica: {updated.get('data_modifica', 'N/A')}\n\n"
                        )
                        
                        if modify_data['tipo'] == 'semplice':
                            response_text += "💡 Modifica completata (solo esito)"
                        elif modify_data['tipo'] == 'completo':
                            response_text += "💡 Modifica completa di tutti i campi"
                        elif modify_data['tipo'] == 'campo_singolo':
                            campo_modificato = list(modify_data['updates'].keys())[0]
                            response_text += f"💡 Modificato solo il campo: {campo_modificato.upper()}"
                        
                        await update.message.reply_text(response_text)
                    else:
                        await update.message.reply_text("❌ Errore durante la modifica!")
                else:
                    await update.message.reply_text(
                        "❌ Formato modifica non valido!\n\n"
                        "📝 **Formati supportati:**\n\n"
                        "• `MODIFICA-ID-ESITO` (solo esito)\n"
                        "• `MODIFICA-ID-RISULTATO-QUOTA-IMPORTO-ESITO` (tutto)\n"
                        "• `MODIFICA-ID-CAMPO=VALORE` (campo specifico)\n\n"
                        "🔍 Usa /lista per vedere gli ID disponibili"
                    )
            
            # Gestione comando ELIMINA
            elif message_text.upper().startswith(('ELIMINA', 'DELETE')):
                quote_id = self.parse_delete_command(message_text)
                
                if quote_id:
                    existing = self.find_superquote_by_id(quote_id)
                    
                    if not existing:
                        await update.message.reply_text(f"❌ ID {quote_id} non trovato!")
                        return
                    
                    confirm_text = (
                        f"⚠️ **CONFERMA ELIMINAZIONE**\n\n"
                        f"🆔 ID: {existing['quote_id']}\n"
                        f"🎯 {existing['risultato']}\n"
                        f"💰 €{existing['importo']:.2f} x {existing['quota']}\n"
                        f"📊 Esito: {existing['esito']}\n\n"
                        f"❓ Sei sicuro di voler ELIMINARE questa giocata?\n"
                        f"Scrivi: **CONFERMA {quote_id}** per eliminare"
                    )
                    
                    if 'pending_deletions' not in context.chat_data:
                        context.chat_data['pending_deletions'] = {}
                    context.chat_data['pending_deletions'][quote_id] = existing
                    
                    await update.message.reply_text(confirm_text, parse_mode='Markdown')
                else:
                    await update.message.reply_text(
                        "❌ Formato eliminazione non valido!\n\n"
                        "📝 Usa: ELIMINA-ID\n"
                        "Esempio: ELIMINA-A1B2C3D4"
                    )
            
            # Gestione conferma eliminazione
            elif message_text.upper().startswith('CONFERMA'):
                parts = message_text.upper().split()
                if len(parts) == 2 and 'pending_deletions' in context.chat_data:
                    quote_id = parts[1]
                    if quote_id in context.chat_data['pending_deletions']:
                        success = self.delete_superquote(quote_id)
                        if success:
                            await update.message.reply_text(f"✅ Giocata {quote_id} eliminata con successo!")
                            del context.chat_data['pending_deletions'][quote_id]
                        else:
                            await update.message.reply_text("❌ Errore durante l'eliminazione!")
                    else:
                        await update.message.reply_text("❌ ID non valido o conferma scaduta!")
                else:
                    await update.message.reply_text("❌ Formato conferma non valido!")
            
            # Gestione inserimento superquote
            elif message_text.upper().startswith('SQ'):
                superquote = self.parse_superquote(message_text)
                
                if superquote:
                    superquote['registrato_da'] = username
                    superquote['user_id'] = update.message.from_user.id
                    
                    success = self.save_superquote(superquote)
                    
                    if success:
                        await update.message.reply_text(
                            f"✅ Superquote registrata!\n\n"
                            f"🆔 ID: {superquote['quote_id']}\n"
                            f"🎯 Risultato: {superquote['risultato']}\n"
                            f"💰 Quota: {superquote['quota']}\n"
                            f"💵 Importo: €{superquote['importo']:.2f}\n"
                            f"🏆 Vincita: €{superquote['vincita']:.2f}\n"
                            f"📊 Esito: {superquote['esito']}\n"
                            f"📅 Data: {superquote['data'][:16]}\n\n"
                            f"💡 Per modificare usa:\n"
                            f"• MODIFICA-{superquote['quote_id']}-ESITO\n"
                            f"• MODIFICA-{superquote['quote_id']}-RISULTATO-QUOTA-IMPORTO-ESITO\n"
                            f"• MODIFICA-{superquote['quote_id']}-CAMPO=VALORE"
                        )
                    else:
                        await update.message.reply_text("❌ Errore nel salvataggio!")
                else:
                    await update.message.reply_text(
                        "❌ Formato non valido!\n\n"
                        "📝 Usa: SQ-risultato-quota-importo-esito\n\n"
                        "🎯 Esempi:\n"
                        "• SQ-1MILAN-2.00-10.00-VINTA\n"
                        "• SQ-OVER2.5-1.85-15.00-PERSA"
                    )
    
    def parse_superquote(self, text: str) -> Optional[Dict]:
        """Parsing del messaggio per estrarre i dati della superquote"""
        try:
            pattern = r'^SQ-([^-]+)-([0-9.]+)-([0-9.]+)-([^-]+)$'
            match = re.match(pattern, text.strip(), re.IGNORECASE)
            
            if match:
                risultato = match.group(1).upper()
                quota = float(match.group(2))
                importo = float(match.group(3))
                esito = match.group(4).upper()
                
                if esito in ['VINTA', 'VINCITA', 'WIN', 'W']:
                    esito = 'VINTA'
                elif esito in ['PERSA', 'PERDITA', 'LOSS', 'L', 'PERSO']:
                    esito = 'PERSA'
                else:
                    return None
                
                vincita = self.calculate_winning_amount(quota, importo, esito)
                
                return {
                    'quote_id': self.generate_quote_id(),
                    'risultato': risultato,
                    'quota': quota,
                    'importo': importo,
                    'vincita': vincita,
                    'esito': esito,
                    'data': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                }
            
            return None
            
        except Exception as e:
            logger.error(f"Errore parsing superquote: {e}")
            return None
    
    async def show_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Mostra le statistiche delle superquote"""
        try:
            superquotes = self.get_all_superquotes()
            balance_data = self.calculate_balance()
            
            if not superquotes:
                await update.message.reply_text("📊 Nessuna superquote registrata ancora!")
                return
            
            stats_text = "📊 **STATISTICHE SUPERQUOTE**\n\n"
            stats_text += f"🎯 Totale giocate: {balance_data['total_bets']}\n"
            stats_text += f"✅ Vinte: {balance_data['wins']}\n"
            stats_text += f"❌ Perse: {balance_data['losses']}\n"
            
            if balance_data['total_bets'] > 0:
                percentuale_successo = (balance_data['wins'] / balance_data['total_bets']) * 100
                stats_text += f"📈 % Successo: {percentuale_successo:.1f}%\n"
            
            stats_text += f"\n💰 **BILANCIO:**\n"
            stats_text += f"💵 Totale puntato: €{balance_data['total_bet']:.2f}\n"
            stats_text += f"🏆 Totale vinto: €{balance_data['total_winnings']:.2f}\n"
            
            saldo = balance_data['saldo']
            saldo_icon = "🟢" if saldo >= 0 else "🔴"
            stats_text += f"{saldo_icon} **SALDO: €{saldo:.2f}**\n"
            
            await update.message.reply_text(stats_text, parse_mode='Markdown')
            
        except Exception as e:
            logger.error(f"Errore show_stats: {e}")
            await update.message.reply_text("❌ Errore nel caricamento statistiche.")
    
    async def show_list(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Mostra la lista delle superquote recenti"""
        try:
            limit = 15
            if context.args and context.args[0].isdigit():
                limit = min(int(context.args[0]), 50)
            
            superquotes = self.get_recent_activity(limit)
            
            if not superquotes:
                await update.message.reply_text("📝 Nessuna superquote registrata!")
                return
            
            list_text = f"📝 **ULTIME {len(superquotes)} SUPERQUOTE**\n\n"
            
            for sq in superquotes:
                emoji = "✅" if sq['esito'] == 'VINTA' else "❌"
                list_text += f"{emoji} **{sq['quote_id']}** - {sq['data'][:10]}\n"
                list_text += f"🎯 {sq['risultato']}\n"
                list_text += f"💰 €{sq['importo']:.2f} x {sq['quota']} → €{sq['vincita']:.2f}\n\n"
            
            await update.message.reply_text(list_text, parse_mode='Markdown')
            
        except Exception as e:
            logger.error(f"Errore show_list: {e}")
            await update.message.reply_text("❌ Errore nel caricamento lista.")
    
    async def show_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Mostra la guida dei comandi"""
        help_text = """
🤖 **SUPERQUOTE BOT - COMANDI**

🎯 **AGGIUNGERE:**
`SQ-risultato-quota-importo-esito`
Es: `SQ-1MILAN-2.50-10.00-VINTA`

🔧 **MODIFICARE:**
`MODIFICA-ID-ESITO` (solo esito)
`MODIFICA-ID-RISULTATO-QUOTA-IMPORTO-ESITO` (tutto)
`MODIFICA-ID-CAMPO=VALORE` (campo specifico)

🗑️ **ELIMINARE:**
`ELIMINA-ID` oppure `DELETE-ID`

📊 **COMANDI:**
`/start` - Avvia il bot
`/help` - Guida comandi
`/stats` - Statistiche
`/lista` - Lista giocate
`/lista 10` - Lista con limite
`/grafico` - Grafico andamento
`/export` - Esporta in CSV

💡 **TIP:** Usa `/lista` per vedere gli ID delle giocate!
        """
        
        await update.message.reply_text(help_text, parse_mode='Markdown')
    
    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Gestisce il comando /start"""
        welcome_text = """
🤖 **BENVENUTO NEL SUPERQUOTE BOT!**

🎯 Tieni traccia delle tue superquote condivise.

📝 **COME INIZIARE:**
1. Aggiungi una giocata: `SQ-risultato-quota-importo-esito`
2. Visualizza le statistiche: `/stats`
3. Vedi la lista: `/lista`

🔧 **MODIFICHE FLESSIBILI:**
Puoi modificare singoli campi o tutto!

📚 Per tutti i comandi: `/help`
        """
        
        await update.message.reply_text(welcome_text, parse_mode='Markdown')

    async def export_csv(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Esporta tutte le superquote in CSV"""
        try:
            superquotes = self.get_all_superquotes()
            
            if not superquotes:
                await update.message.reply_text("📊 Nessuna superquote da esportare!")
                return
            
            output = io.StringIO()
            fieldnames = ['ID', 'Data', 'Risultato', 'Quota', 'Importo', 'Vincita', 'Esito', 'Registrato da']
            writer = csv.DictWriter(output, fieldnames=fieldnames)
            
            writer.writeheader()
            for sq in superquotes:
                writer.writerow({
                    'ID': sq['quote_id'],
                    'Data': sq['data'],
                    'Risultato': sq['risultato'],
                    'Quota': sq['quota'],
                    'Importo': sq['importo'],
                    'Vincita': sq['vincita'],
                    'Esito': sq['esito'],
                    'Registrato da': sq.get('registrato_da', 'N/A')
                })
            
            csv_data = output.getvalue()
            output.close()
            
            csv_buffer = io.BytesIO()
            csv_buffer.write(csv_data.encode('utf-8'))
            csv_buffer.seek(0)
            
            await update.message.reply_document(
                document=csv_buffer,
                filename=f"superquote_export_{datetime.now().strftime('%Y%m%d_%H%M')}.csv",
                caption=f"📊 Esportazione di {len(superquotes)} superquote"
            )
            
            logger.info(f"📤 CSV esportato con {len(superquotes)} righe")
            
        except Exception as e:
            logger.error(f"Errore export_csv: {e}")
            await update.message.reply_text("❌ Errore nell'esportazione. Riprova più tardi.")

def main():
    """Funzione principale per avviare il bot"""
    
    # Configurazione
    TOKEN = os.getenv('BOT_TOKEN')
    MONGODB_URI = os.getenv('MONGO_URL')
    
    if not TOKEN:
        logger.error("❌ TELEGRAM_TOKEN non trovato nelle variabili d'ambiente!")
        return
    
    if not MONGODB_URI:
        logger.error("❌ MONGODB_URI non trovato nelle variabili d'ambiente!")
        return
    
    try:
        # Inizializza il bot
        bot = SuperquoteBot(TOKEN, MONGODB_URI)
        application = Application.builder().token(TOKEN).build()
        
        # Aggiungi gestori comandi
        application.add_handler(CommandHandler("start", bot.start))
        application.add_handler(CommandHandler("help", bot.show_help))
        application.add_handler(CommandHandler("stats", bot.show_stats))
        application.add_handler(CommandHandler("lista", bot.show_list))
        application.add_handler(CommandHandler("grafico", bot.generate_profit_graph))
        application.add_handler(CommandHandler("export", bot.export_csv))
        
        # Gestore messaggi generici (deve essere l'ultimo)
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, bot.handle_message))
        
        logger.info("🤖 Bot avviato con successo!")
        print("=" * 50)
        print("SUPERQUOTE BOT AVVIATO!")
        print("Comandi disponibili: /start, /help, /stats, /lista, /grafico")
        print("=" * 50)
        
        # Avvia il bot
        application.run_polling()
        
    except ConnectionFailure as e:
        logger.error(f"❌ Errore di connessione MongoDB: {e}")
        print(f"❌ ERRORE CRITICO: {e}")
    except Exception as e:
        logger.error(f"❌ Errore nell'avvio del bot: {e}")
        print(f"❌ ERRORE CRITICO: {e}")

if __name__ == '__main__':
    main()