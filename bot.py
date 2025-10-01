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
                serverSelectionTimeoutMS=10000,  # Timeout più lungo
                connectTimeoutMS=20000,
                socketTimeoutMS=20000,
                maxPoolSize=10,  # Pool ridotto per risparmiare risorse
                retryWrites=True,
                w='majority'
            )
            
            # Test della connessione
            self.client.admin.command('ping')
            
            self.db = self.client[self.db_name]
            self.collection = self.db[self.collection_name]
            
            # Verifica spazio disponibile prima di creare indici
            stats = self.db.command("dbStats")
            logger.info(f"📊 Spazio DB - Utilizzato: {stats.get('dataSize', 0)} bytes")
            
            logger.info("✅ Connesso a MongoDB con successo!")
            
            # Crea indici solo se necessario (per risparmiare spazio)
            try:
                existing_indexes = list(self.collection.list_indexes())
                index_names = [idx['name'] for idx in existing_indexes]
                
                if 'data_-1' not in index_names:
                    self.collection.create_index([("data", -1)])
                if 'user_id_1' not in index_names:
                    self.collection.create_index([("user_id", 1)])
                if 'esito_1' not in index_names:
                    self.collection.create_index([("esito", 1)])
                if 'quote_id_1' not in index_names:
                    self.collection.create_index([("quote_id", 1)])  # Nuovo indice per ID univoco
                    
                logger.info("📋 Indici database verificati/creati")
            except Exception as idx_error:
                logger.warning(f"⚠️ Errore creazione indici (continuo comunque): {idx_error}")
            
        except ServerSelectionTimeoutError as e:
            logger.error(f"⏱ Timeout connessione MongoDB: {e}")
            raise ConnectionFailure(f"Timeout connessione a MongoDB: verificare che il servizio sia attivo")
        except Exception as e:
            error_msg = str(e)
            if "OutOfDiskSpace" in error_msg or "14031" in error_msg:
                logger.error("💾 ERRORE SPAZIO DISCO ESAURITO!")
                raise ConnectionFailure(
                    "MongoDB ha esaurito lo spazio disco su Railway. "
                    "Soluzioni: 1) Upgrade piano Railway, 2) Usa MongoDB Atlas gratuito, "
                    "3) Cancella dati vecchi dal database"
                )
            else:
                logger.error(f"❌ Errore connessione MongoDB: {e}")
                raise ConnectionFailure(f"Impossibile connettersi a MongoDB: {e}")
    
    def generate_quote_id(self) -> str:
        """Genera un ID univoco per la giocata (8 caratteri)"""
        return str(uuid.uuid4())[:8].upper()
    
    def get_all_superquotes(self) -> List[Dict]:
        """Ottiene tutte le superquote ordinate per data (più recenti prima)"""
        try:
            # Limite per evitare sovraccarichi
            cursor = self.collection.find({}).sort('data', -1).limit(1000)
            data = list(cursor)
            
            # Converti ObjectId to string per compatibilità 
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
        """Salva una superquote in MongoDB con controllo spazio"""
        try:
            # Verifica spazio prima di salvare
            stats = self.db.command("dbStats")
            data_size = stats.get('dataSize', 0)
            
            # Se il DB è troppo grande (>100MB), avvisa
            if data_size > 100_000_000:
                logger.warning(f"⚠️ Database grande: {data_size/1_000_000:.1f}MB")
            
            # Crea una copia per non modificare l'originale
            sq_copy = superquote.copy()
            sq_copy.pop('_id', None)
            
            result = self.collection.insert_one(sq_copy)
            logger.info(f"Superquote salvata con ID: {result.inserted_id}")
            return True
            
        except Exception as e:
            error_msg = str(e)
            if "OutOfDiskSpace" in error_msg:
                logger.error("💾 Spazio disco esaurito durante il salvataggio!")
                return False
            logger.error(f"Errore nel salvataggio: {e}")
            return False
    
    def update_superquote(self, quote_id: str, updates: Dict) -> bool:
        """Aggiorna una superquote esistente con nuovi dati"""
        try:
            # Trova la superquote esistente
            existing = self.find_superquote_by_id(quote_id)
            if not existing:
                return False
            
            # Prepara i campi da aggiornare
            update_fields = {}
            
            # Gestisci l'aggiornamento del risultato
            if 'risultato' in updates:
                update_fields['risultato'] = updates['risultato']
            
            # Gestisci l'aggiornamento della quota
            if 'quota' in updates:
                new_quota = float(updates['quota'])
                update_fields['quota'] = new_quota
            
            # Gestisci l'aggiornamento dell'importo
            if 'importo' in updates:
                new_importo = float(updates['importo'])
                update_fields['importo'] = new_importo
            
            # Gestisci l'aggiornamento dell'esito
            if 'esito' in updates:
                new_esito = updates['esito'].upper()
                if new_esito in ['VINTA', 'VINCITA', 'WIN', 'W']:
                    new_esito = 'VINTA'
                elif new_esito in ['PERSA', 'PERDITA', 'LOSS', 'L', 'PERSO']:
                    new_esito = 'PERSA'
                else:
                    return False
                update_fields['esito'] = new_esito
            
            # Calcola la nuova vincita se sono cambiati quota, importo o esito
            if any(field in updates for field in ['quota', 'importo', 'esito']):
                final_quota = update_fields.get('quota', existing['quota'])
                final_importo = update_fields.get('importo', existing['importo'])
                final_esito = update_fields.get('esito', existing['esito'])
                
                update_fields['vincita'] = self.calculate_winning_amount(
                    final_quota, final_importo, final_esito
                )
            
            # Aggiungi data di modifica
            update_fields['data_modifica'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            
            # Esegui l'aggiornamento nel database
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
        - MODIFICA-ID-ESITO (compatibilità)
        - MODIFICA-ID-RISULTATO-QUOTA-IMPORTO-ESITO (nuovo)
        - MODIFICA-ID-CAMPO=VALORE (modifica singolo campo)
        """
        text_clean = text.strip().upper()
        
        # Formato semplice: MODIFICA-ID-ESITO (compatibilità)
        simple_pattern = r'^MODIFICA-([A-Z0-9]{8})-([^-]+)$'
        simple_match = re.match(simple_pattern, text_clean)
        
        if simple_match:
            quote_id = simple_match.group(1)
            esito = simple_match.group(2)
            
            # Normalizza l'esito
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
            
            # Normalizza l'esito
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
    
    def get_user_stats(self, user_id: int = None) -> Dict:
        """Calcola le statistiche per un utente specifico"""
        try:
            query = {}
            if user_id:
                query = {"user_id": user_id}
            
            superquotes = list(self.collection.find(query))
            
            if not superquotes:
                return {
                    'total_bets': 0,
                    'wins': 0,
                    'losses': 0,
                    'total_bet': 0.0,
                    'total_winnings': 0.0,
                    'saldo': 0.0
                }
            
            total_bet = sum(sq['importo'] for sq in superquotes)
            total_winnings = sum(sq['vincita'] for sq in superquotes if sq['esito'] == 'VINTA')
            wins = len([sq for sq in superquotes if sq['esito'] == 'VINTA'])
            losses = len([sq for sq in superquotes if sq['esito'] == 'PERSA'])
            balance = total_winnings - total_bet
            
            return {
                'total_bets': len(superquotes),
                'wins': wins,
                'losses': losses,
                'total_bet': total_bet,
                'total_winnings': total_winnings,
                'saldo': balance
            }
        except Exception as e:
            logger.error(f"Errore calcolo statistiche utente: {e}")
            return {
                'total_bets': 0,
                'wins': 0,
                'losses': 0,
                'total_bet': 0.0,
                'total_winnings': 0.0,
                'saldo': 0.0
            }
    
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
            
            # Ordina le superquote per data (dalla più vecchia alla più recente)
            superquotes_sorted = sorted(superquotes, key=lambda x: x['data'])
            
            # Calcola l'andamento cumulativo del saldo
            dates = []
            cumulative_profit = []
            current_balance = 0
            
            for sq in superquotes_sorted:
                # Converti la stringa data in datetime
                try:
                    date_obj = datetime.strptime(sq['data'], '%Y-%m-%d %H:%M:%S')
                except ValueError:
                    # Se il formato è diverso, prova un altro formato
                    try:
                        date_obj = datetime.strptime(sq['data'][:10], '%Y-%m-%d')
                    except:
                        date_obj = datetime.now()
                
                dates.append(date_obj)
                
                # Aggiorna il saldo corrente
                if sq['esito'] == 'VINTA':
                    current_balance += sq['vincita'] - sq['importo']  # Vincita netta
                else:
                    current_balance -= sq['importo']  # Perdita
                
                cumulative_profit.append(current_balance)
            
            # Crea il grafico
            plt.figure(figsize=(10, 6))
            
            # Colore del grafico in base al saldo finale
            line_color = 'green' if current_balance >= 0 else 'red'
            fill_color = 'lightgreen' if current_balance >= 0 else 'lightcoral'
            
            # Traccia l'andamento
            plt.plot(dates, cumulative_profit, color=line_color, linewidth=2.5, label='Saldo')
            plt.fill_between(dates, cumulative_profit, alpha=0.3, color=fill_color)
            
            # Linea dello zero
            plt.axhline(y=0, color='black', linestyle='-', alpha=0.3, linewidth=1)
            
            # Formatta le date
            plt.gca().xaxis.set_major_formatter(mdates.DateFormatter('%d/%m'))
            plt.gca().xaxis.set_major_locator(mdates.WeekdayLocator(interval=1))
            plt.gcf().autofmt_xdate()
            
            # Titoli e labels
            plt.title('📈 Andamento delle Vincite Cumulative', fontsize=14, fontweight='bold')
            plt.xlabel('Data')
            plt.ylabel('Saldo (€)')
            plt.grid(True, alpha=0.3)
            plt.legend()
            
            # Aggiungi annotazione con il saldo finale
            final_balance_text = f"Saldo finale: €{current_balance:.2f}"
            plt.annotate(final_balance_text, 
                        xy=(1, 0), xycoords='axes fraction',
                        xytext=(-10, 10), textcoords='offset points',
                        ha='right', va='bottom',
                        bbox=dict(boxstyle='round,pad=0.5', fc='yellow', alpha=0.7),
                        fontsize=10)
            
            # Salva il grafico in memoria
            graph_buffer = io.BytesIO()
            plt.savefig(graph_buffer, format='png', dpi=100, bbox_inches='tight')
            graph_buffer.seek(0)
            plt.close()
            
            # Prepara il messaggio con le statistiche
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
            
            # Invia il grafico
            await update.message.reply_photo(
                photo=graph_buffer,
                caption=caption,
                parse_mode='Markdown'
            )
            
            logger.info(f"📈 Grafico inviato per {len(superquotes)} giocate")
            
        except Exception as e:
            logger.error(f"Errore nella generazione del grafico: {e}")
            await update.message.reply_text(
                "❌ Errore nella generazione del grafico. Riprova più tardi.\n"
                f"Errore: {str(e)}"
            )
    
    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Gestisce i messaggi in arrivo"""
        if update.message and update.message.text:
            message_text = update.message.text.strip()
            username = update.message.from_user.username or update.message.from_user.first_name or "Anonimo"
            
            # Gestione comando MODIFICA
            if message_text.upper().startswith('MODIFICA'):
                modify_data = self.parse_modify_command(message_text)
                
                if modify_data:
                    # Trova la superquote esistente
                    existing = self.find_superquote_by_id(modify_data['quote_id'])
                    
                    if not existing:
                        await update.message.reply_text(
                            f"❌ ID {modify_data['quote_id']} non trovato!\n\n"
                            f"🔍 Usa /lista per vedere gli ID delle giocate"
                        )
                        return
                    
                    # Aggiorna la superquote
                    success = self.update_superquote(
                        modify_data['quote_id'], 
                        modify_data['updates']
                    )
                    
                    if success:
                        # Ricarica i dati aggiornati
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
                        "   Es: MODIFICA-A1B2C3D4-VINTA\n\n"
                        "• `MODIFICA-ID-RISULTATO-QUOTA-IMPORTO-ESITO` (tutto)\n"
                        "   Es: MODIFICA-A1B2C3D4-1MILAN-2.50-15.00-VINTA\n\n"
                        "• `MODIFICA-ID-CAMPO=VALORE` (campo specifico)\n"
                        "   Es: MODIFICA-A1B2C3D4-QUOTA=2.50\n"
                        "   Es: MODIFICA-A1B2C3D4-IMPORTO=20.00\n"
                        "   Es: MODIFICA-A1B2C3D4-RISULTATO=OVER2.5\n\n"
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
                    
                    # Mostra conferma prima di eliminare
                    confirm_text = (
                        f"⚠️ **CONFERMA ELIMINAZIONE**\n\n"
                        f"🆔 ID: {existing['quote_id']}\n"
                        f"🎯 {existing['risultato']}\n"
                        f"💰 €{existing['importo']:.2f} x {existing['quota']}\n"
                        f"📊 Esito: {existing['esito']}\n\n"
                        f"❓ Sei sicuro di voler ELIMINARE questa giocata?\n"
                        f"Scrivi: **CONFERMA {quote_id}** per eliminare"
                    )
                    
                    # Salva l'ID per la conferma nel context
                    if 'pending_deletions' not in context.chat_data:
                        context.chat_data['pending_deletions'] = {}
                    context.chat_data['pending_deletions'][quote_id] = existing
                    
                    await update.message.reply_text(confirm_text, parse_mode='Markdown')
                else:
                    await update.message.reply_text(
                        "❌ Formato eliminazione non valido!\n\n"
                        "📝 Usa: ELIMINA-ID\n"
                        "Esempio: ELIMINA-A1B2C3D4\n\n"
                        "🔍 Usa /lista per vedere gli ID"
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
                            f"💡 Per modificare usa uno di questi formati:\n"
                            f"• MODIFICA-{superquote['quote_id']}-ESITO\n"
                            f"• MODIFICA-{superquote['quote_id']}-RISULTATO-QUOTA-IMPORTO-ESITO\n"
                            f"• MODIFICA-{superquote['quote_id']}-CAMPO=VALORE"
                        )
                    else:
                        await update.message.reply_text(
                            "❌ Errore nel salvataggio!\n"
                            "💾 Possibile problema di spazio disco.\n"
                            "Contatta l'admin del bot."
                        )
                else:
                    await update.message.reply_text(
                        "❌ Formato non valido!\n\n"
                        "📝 Usa il formato: SQ-risultato-quota-importo-esito\n\n"
                        "🎯 Esempi corretti:\n"
                        "• SQ-1MILAN-2.00-10.00-VINTA\n"
                        "• SQ-OVER2.5-1.85-15.00-PERSA\n"
                        "• SQ-COMBO-3.20-5.00-VINTA\n\n"
                        "⚠️ ATTENZIONE: Il terzo numero è l'IMPORTO GIOCATO!"
                    )
    
    async def show_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Mostra le statistiche delle superquote con saldo"""
        try:
            superquotes = self.get_all_superquotes()
            balance_data = self.calculate_balance()
            
            if not superquotes:
                await update.message.reply_text("📊 Nessuna superquote registrata ancora!")
                return
            
            stats_text = "📊 **STATISTICHE SUPERQUOTE CONDIVISE**\n\n"
            
            total_superquote = balance_data['total_bets']
            vinte = balance_data['wins']
            perse = balance_data['losses']
            
            stats_text += f"🎯 Totale superquote: {total_superquote}\n"
            stats_text += f"✅ Vinte: {vinte}\n"
            stats_text += f"❌ Perse: {perse}\n"
            
            if total_superquote > 0:
                percentuale_successo = (vinte / total_superquote) * 100
                stats_text += f"📈 % Successo: {percentuale_successo:.1f}%\n"
            
            stats_text += f"\n💰 **BILANCIO ECONOMICO:**\n"
            stats_text += f"💵 Totale puntato: €{balance_data['total_bet']:.2f}\n"
            stats_text += f"🏆 Totale vinto: €{balance_data['total_winnings']:.2f}\n"
            
            saldo = balance_data['saldo']
            saldo_icon = "🟢" if saldo >= 0 else "🔴"
            saldo_text = "POSITIVO" if saldo >= 0 else "NEGATIVO"
            
            stats_text += f"{saldo_icon} **SALDO: €{saldo:.2f} ({saldo_text})**\n"
            
            if total_superquote > 0:
                importo_medio = balance_data['total_bet'] / total_superquote
                quota_media = sum(sq['quota'] for sq in superquotes) / total_superquote
                
                stats_text += f"\n📊 **MEDIE:**\n"
                stats_text += f"💵 Importo medio: €{importo_medio:.2f}\n"
                stats_text += f"🎲 Quota media: {quota_media:.2f}\n"
            
            if superquotes:
                best_win = max(superquotes, key=lambda x: x['vincita'])
                stats_text += f"\n🏆 **MIGLIOR VINCITA:**\n"
                stats_text += f"🎯 {best_win['risultato']}\n"
                stats_text += f"💰 €{best_win['importo']:.2f} x {best_win['quota']} → €{best_win['vincita']:.2f}\n"
                stats_text += f"📅 {best_win['data'][:10]}\n"
                
                won_bets = [sq for sq in superquotes if sq['esito'] == 'VINTA']
                if won_bets:
                    highest_won_odds = max(won_bets, key=lambda x: x['quota'])
                    stats_text += f"\n🎰 **QUOTA PIÙ ALTA VINTA:**\n"
                    stats_text += f"🎯 {highest_won_odds['risultato']}\n"
                    stats_text += f"💰 Quota {highest_won_odds['quota']}\n"
            
            await update.message.reply_text(stats_text, parse_mode='Markdown')
            
        except Exception as e:
            logger.error(f"Errore show_stats: {e}")
            await update.message.reply_text("❌ Errore nel caricamento statistiche. Riprova più tardi.")
    
    async def show_list(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Mostra la lista delle superquote recenti con ID"""
        try:
            # Controlla se è stato passato un limite
            limit = 15
            if context.args and context.args[0].isdigit():
                limit = min(int(context.args[0]), 50)  # Massimo 50 elementi
            
            superquotes = self.get_recent_activity(limit)
            
            if not superquotes:
                await update.message.reply_text("📝 Nessuna superquote registrata ancora!")
                return
            
            list_text = f"📝 **ULTIME {len(superquotes)} SUPERQUOTE**\n\n"
            
            for i, sq in enumerate(superquotes, 1):
                emoji = "✅" if sq['esito'] == 'VINTA' else "❌"
                list_text += f"{emoji} **{sq['quote_id']}** - {sq['data'][:10]}\n"
                list_text += f"🎯 {sq['risultato']}\n"
                list_text += f"💰 €{sq['importo']:.2f} x {sq['quota']} → €{sq['vincita']:.2f}\n"
                
                if sq.get('registrato_da'):
                    list_text += f"👤 {sq['registrato_da']}\n"
                
                if sq.get('data_modifica'):
                    list_text += f"🔄 Modificata: {sq['data_modifica'][:16]}\n"
                
                list_text += "\n"
            
            # Aggiungi istruzioni per la modifica
            list_text += (
                "🔧 **COME MODIFICARE:**\n"
                "• `MODIFICA-ID-ESITO` (solo esito)\n"
                "• `MODIFICA-ID-RISULTATO-QUOTA-IMPORTO-ESITO` (tutto)\n"
                "• `MODIFICA-ID-CAMPO=VALORE` (campo specifico)\n\n"
                "🗑️ **COME ELIMINARE:**\n"
                "• `ELIMINA-ID`\n"
                "• `DELETE-ID`"
            )
            
            await update.message.reply_text(list_text, parse_mode='Markdown')
            
        except Exception as e:
            logger.error(f"Errore show_list: {e}")
            await update.message.reply_text("❌ Errore nel caricamento lista. Riprova più tardi.")
    
    async def show_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Mostra la guida completa dei comandi"""
        help_text = """
🤖 **SUPERQUOTE BOT - GUIDA COMPLETA**

🎯 **AGGIUNGERE UNA GIOCATA:**
`SQ-risultato-quota-importo-esito`
Esempio: `SQ-1MILAN-2.50-10.00-VINTA`

🔧 **MODIFICARE UNA GIOCATA:**
1. Solo esito: `MODIFICA-ID-ESITO`
   Es: `MODIFICA-A1B2C3D4-VINTA`

2. Tutti i campi: `MODIFICA-ID-RISULTATO-QUOTA-IMPORTO-ESITO`
   Es: `MODIFICA-A1B2C3D4-1MILAN-2.50-15.00-VINTA`

3. Campo specifico: `MODIFICA-ID-CAMPO=VALORE`
   Es: `MODIFICA-A1B2C3D4-QUOTA=2.50`
   Es: `MODIFICA-A1B2C3D4-IMPORTO=20.00`
   Es: `MODIFICA-A1B2C3D4-RISULTATO=OVER2.5`

🗑️ **ELIMINARE UNA GIOCATA:**
`ELIMINA-ID` oppure `DELETE-ID`
Esempio: `ELIMINA-A1B2C3D4`

📊 **COMANDI DISPONIBILI:**
`/start` - Avvia il bot
`/help` - Mostra questa guida
`/stats` - Statistiche complete
`/lista` - Lista ultime giocate
`/lista 10` - Lista con limite
`/grafico` - Grafico andamento
`/export` - Esporta in CSV
`/userstats` - Statistiche personali

💡 **TIP:**
• Usa `/lista` per vedere gli ID delle giocate
• Gli esiti possono essere: VINTA, PERSA
• Le quote usano il punto come separatore: 2.50
• Gli importi sono in euro: 10.00

🔍 **ESEMPI PRATICI:**
Aggiungi: `SQ-OVER2.5-1.85-15.00-VINTA`
Modifica: `MODIFICA-ABC12345-PERSA`
Elimina: `ELIMINA-ABC12345`
        """
        
        await update.message.reply_text(help_text, parse_mode='Markdown')
    
    async def export_csv(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Esporta tutte le superquote in formato CSV"""
        try:
            superquotes = self.get_all_superquotes()
            
            if not superquotes:
                await update.message.reply_text("📊 Nessuna superquote da esportare!")
                return
            
            # Crea il CSV in memoria
            output = io.StringIO()
            fieldnames = ['ID', 'Data', 'Risultato', 'Quota', 'Importo', 'Vincita', 'Esito', 'Registrato da', 'Ultima modifica']
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
                    'Registrato da': sq.get('registrato_da', 'N/A'),
                    'Ultima modifica': sq.get('data_modifica', 'N/A')
                })
            
            csv_data = output.getvalue()
            output.close()
            
            # Crea file in memoria
            csv_buffer = io.BytesIO()
            csv_buffer.write(csv_data.encode('utf-8'))
            csv_buffer.seek(0)
            
            # Invia il file
            await update.message.reply_document(
                document=csv_buffer,
                filename=f"superquote_export_{datetime.now().strftime('%Y%m%d_%H%M')}.csv",
                caption=f"📊 Esportazione di {len(superquotes)} superquote"
            )
            
            logger.info(f"📤 CSV esportato con {len(superquotes)} righe")
            
        except Exception as e:
            logger.error(f"Errore export_csv: {e}")
            await update.message.reply_text("❌ Errore nell'esportazione. Riprova più tardi.")
    
    async def user_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Mostra le statistiche personali dell'utente"""
        try:
            user_id = update.message.from_user.id
            username = update.message.from_user.username or update.message.from_user.first_name
            
            stats = self.get_user_stats(user_id)
            
            if stats['total_bets'] == 0:
                await update.message.reply_text(
                    f"👤 {username}, non hai ancora registrato nessuna superquote!"
                )
                return
            
            stats_text = f"👤 **STATISTICHE PERSONALI - {username}**\n\n"
            stats_text += f"🎯 Totale giocate: {stats['total_bets']}\n"
            stats_text += f"✅ Vinte: {stats['wins']}\n"
            stats_text += f"❌ Perse: {stats['losses']}\n"
            
            if stats['total_bets'] > 0:
                percentuale_successo = (stats['wins'] / stats['total_bets']) * 100
                stats_text += f"📈 % Successo: {percentuale_successo:.1f}%\n"
            
            stats_text += f"\n💰 **BILANCIO:**\n"
            stats_text += f"💵 Totale puntato: €{stats['total_bet']:.2f}\n"
            stats_text += f"🏆 Totale vinto: €{stats['total_winnings']:.2f}\n"
            
            saldo = stats['saldo']
            saldo_icon = "🟢" if saldo >= 0 else "🔴"
            saldo_text = "POSITIVO" if saldo >= 0 else "NEGATIVO"
            
            stats_text += f"{saldo_icon} **SALDO: €{saldo:.2f} ({saldo_text})**\n"
            
            await update.message.reply_text(stats_text, parse_mode='Markdown')
            
        except Exception as e:
            logger.error(f"Errore user_stats: {e}")
            await update.message.reply_text("❌ Errore nel caricamento statistiche personali.")
    
    def parse_superquote(self, text: str) -> Optional[Dict]:
        """Parsing del messaggio per estrarre i dati della superquote"""
        try:
            # Pattern per SQ-risultato-quota-importo-esito
            pattern = r'^SQ-([^-]+)-([0-9.]+)-([0-9.]+)-([^-]+)$'
            match = re.match(pattern, text.strip(), re.IGNORECASE)
            
            if match:
                risultato = match.group(1).upper()
                quota = float(match.group(2))
                importo = float(match.group(3))
                esito = match.group(4).upper()
                
                # Normalizza l'esito
                if esito in ['VINTA', 'VINCITA', 'WIN', 'W']:
                    esito = 'VINTA'
                elif esito in ['PERSA', 'PERDITA', 'LOSS', 'L', 'PERSO']:
                    esito = 'PERSA'
                else:
                    return None
                
                # Calcola la vincita
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
    
    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Gestisce il comando /start"""
        welcome_text = """
🤖 **BENVENUTO NEL SUPERQUOTE BOT!**

🎯 Questo bot ti aiuta a tenere traccia delle tue superquote condivise con gli amici.

📝 **COME INIZIARE:**
1. Aggiungi una giocata: `SQ-risultato-quota-importo-esito`
   Esempio: `SQ-1MILAN-2.50-10.00-VINTA`

2. Visualizza le statistiche: `/stats`

3. Vedi la lista: `/lista`

🔧 **MODIFICHE FLESSIBILI:**
Puoi modificare singoli campi o tutto:
• Solo esito: `MODIFICA-ID-ESITO`
• Campo specifico: `MODIFICA-ID-CAMPO=VALORE`
• Tutto: `MODIFICA-ID-RISULTATO-QUOTA-IMPORTO-ESITO`

📚 Per tutti i comandi: `/help`

💡 **SUGGERIMENTO:** Inizia con `/lista` per vedere le giocate esistenti!
        """
        
        await update.message.reply_text(welcome_text, parse_mode='Markdown')

def main():
    """Funzione principale per avviare il bot"""
    
    # Configurazione
    TOKEN = os.getenv('TELEGRAM_TOKEN')
    MONGODB_URI = os.getenv('MONGODB_URI')
    
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
        application.add_handler(CommandHandler("userstats", bot.user_stats))
        
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
        print("💡 Soluzioni possibili:")
        print("1. Verifica che MongoDB sia attivo su Railway")
        print("2. Controlla la MONGODB_URI nelle variabili d'ambiente")
        print("3. Se hai esaurito lo spazio, usa MongoDB Atlas gratuito")
    except Exception as e:
        logger.error(f"❌ Errore nell'avvio del bot: {e}")
        print(f"❌ ERRORE CRITICO: {e}")

if __name__ == '__main__':
    main()