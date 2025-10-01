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
    
    def update_superquote_outcome(self, quote_id: str, new_outcome: str) -> bool:
        """Aggiorna l'esito di una superquote esistente"""
        try:
            # Trova la superquote
            existing = self.find_superquote_by_id(quote_id)
            if not existing:
                return False
            
            # Normalizza il nuovo esito
            if new_outcome.upper() in ['VINTA', 'VINCITA', 'WIN', 'W']:
                new_outcome = 'VINTA'
            elif new_outcome.upper() in ['PERSA', 'PERDITA', 'LOSS', 'L', 'PERSO']:
                new_outcome = 'PERSA'
            else:
                return False
            
            # Calcola la nuova vincita
            new_vincita = self.calculate_winning_amount(
                existing['quota'], 
                existing['importo'], 
                new_outcome
            )
            
            # Aggiorna nel database
            result = self.collection.update_one(
                {"quote_id": quote_id.upper()},
                {
                    "$set": {
                        "esito": new_outcome,
                        "vincita": new_vincita,
                        "data_modifica": datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                    }
                }
            )
            
            return result.modified_count > 0
            
        except Exception as e:
            logger.error(f"Errore nell'aggiornamento: {e}")
            return False
    
    def get_wins(self) -> List[Dict]:
        """Ottiene solo le superquote vinte"""
        try:
            cursor = self.collection.find({"esito": "VINTA"}).sort('data', -1).limit(100)
            wins = list(cursor)
            for item in wins:
                item['_id'] = str(item['_id'])
            return wins
        except Exception as e:
            logger.error(f"Errore nel recupero vincite: {e}")
            return []

    def parse_superquote(self, text: str) -> Optional[Dict]:
        """
        Parsing del messaggio superquote
        Formato aggiornato: SQ-risultato-quota-importo-esito
        Esempio: SQ-1MILAN-2.00-10.00-VINTA
        """
        text_clean = text.strip()
        
        # Pattern regex più flessibile
        pattern = r'^SQ-([^-]+)-([0-9]+(?:\.[0-9]+)?)-([0-9]+(?:\.[0-9]+)?)-([^-]+)$'
        match = re.match(pattern, text_clean, re.IGNORECASE)
        
        if match:
            risultato = match.group(1).strip()
            try:
                quota = float(match.group(2))
                importo = float(match.group(3))  # Ora è l'importo giocato
            except ValueError:
                return None
            
            esito = match.group(4).strip().upper()
            
            # Normalizza l'esito
            if esito in ['VINTA', 'VINCITA', 'WIN', 'W']:
                esito = 'VINTA'
            elif esito in ['PERSA', 'PERDITA', 'LOSS', 'L', 'PERSO']:
                esito = 'PERSA'
            else:
                return None
            
            # Calcola la vincita
            vincita = self.calculate_winning_amount(quota, importo, esito)
            
            # Genera ID univoco
            quote_id = self.generate_quote_id()
            
            return {
                'quote_id': quote_id,
                'risultato': risultato,
                'quota': quota,
                'importo': importo,
                'vincita': vincita,
                'esito': esito,
                'data': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'messaggio_originale': text_clean
            }
        return None
    
    def parse_modify_command(self, text: str) -> Optional[Dict]:
        """
        Parsing del comando di modifica
        Formato: MODIFICA-ID-ESITO
        Esempio: MODIFICA-A1B2C3D4-VINTA
        """
        text_clean = text.strip()
        
        pattern = r'^MODIFICA-([A-Z0-9]{8})-([^-]+)$'
        match = re.match(pattern, text_clean, re.IGNORECASE)
        
        if match:
            quote_id = match.group(1).upper()
            esito = match.group(2).strip().upper()
            
            # Normalizza l'esito
            if esito in ['VINTA', 'VINCITA', 'WIN', 'W']:
                esito = 'VINTA'
            elif esito in ['PERSA', 'PERDITA', 'LOSS', 'L', 'PERSO']:
                esito = 'PERSA'
            else:
                return None
            
            return {
                'quote_id': quote_id,
                'nuovo_esito': esito
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
                    
                    # Aggiorna l'esito
                    success = self.update_superquote_outcome(
                        modify_data['quote_id'], 
                        modify_data['nuovo_esito']
                    )
                    
                    if success:
                        # Ricarica i dati aggiornati
                        updated = self.find_superquote_by_id(modify_data['quote_id'])
                        
                        await update.message.reply_text(
                            f"✅ Giocata modificata!\n\n"
                            f"🆔 ID: {updated['quote_id']}\n"
                            f"🎯 Risultato: {updated['risultato']}\n"
                            f"💰 Quota: {updated['quota']}\n"
                            f"💵 Importo: €{updated['importo']:.2f}\n"
                            f"🏆 Vincita: €{updated['vincita']:.2f}\n"
                            f"📊 Esito: {updated['esito']}\n"
                            f"📅 Modificata: {updated.get('data_modifica', 'N/A')}"
                        )
                    else:
                        await update.message.reply_text("❌ Errore durante la modifica!")
                else:
                    await update.message.reply_text(
                        "❌ Formato modifica non valido!\n\n"
                        "📝 Usa il formato: MODIFICA-ID-ESITO\n\n"
                        "🎯 Esempi:\n"
                        "• MODIFICA-A1B2C3D4-VINTA\n"
                        "• MODIFICA-E5F6G7H8-PERSA"
                    )
            
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
                            f"💡 Per modificare usa: MODIFICA-{superquote['quote_id']}-ESITO"
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
            superquotes = self.get_all_superquotes()
            
            if not superquotes:
                await update.message.reply_text("📝 Nessuna superquote registrata ancora!")
                return
            
            list_text = "📝 **ULTIME SUPERQUOTE**\n\n"
            
            for sq in superquotes[:15]:
                icon = "✅" if sq['esito'] == 'VINTA' else "❌"
                data_breve = sq['data'][:10]
                
                list_text += f"{icon} **{sq['risultato']}** (ID: `{sq['quote_id']}`)\n"
                list_text += f"    💰 €{sq['importo']:.2f} x {sq['quota']} → €{sq['vincita']:.2f} | {data_breve}\n\n"
            
            if len(superquotes) > 15:
                list_text += f"📋 ... e altre {len(superquotes) - 15} superquote\n"
                list_text += "Usa /export per il file completo\n\n"
            
            list_text += "💡 Per modificare: MODIFICA-ID-ESITO"
            
            await update.message.reply_text(list_text, parse_mode='Markdown')
            
        except Exception as e:
            logger.error(f"Errore show_list: {e}")
            await update.message.reply_text("❌ Errore nel caricamento lista. Riprova più tardi.")
    
    async def show_recent_wins(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Mostra solo le vincite recenti"""
        try:
            wins = self.get_wins()
            
            if not wins:
                await update.message.reply_text("🎯 Nessuna vincita registrata ancora!")
                return
            
            list_text = "🏆 **ULTIME VINCITE**\n\n"
            
            for sq in wins[:12]:
                data_breve = sq['data'][:10]
                list_text += f"✅ **{sq['risultato']}** (ID: `{sq['quote_id']}`)\n"
                list_text += f"    💰 €{sq['importo']:.2f} x {sq['quota']} → €{sq['vincita']:.2f} | {data_breve}\n\n"
            
            if len(wins) > 12:
                list_text += f"🎯 Totale vincite: {len(wins)}"
            
            await update.message.reply_text(list_text, parse_mode='Markdown')
            
        except Exception as e:
            logger.error(f"Errore show_recent_wins: {e}")
            await update.message.reply_text("❌ Errore nel caricamento vincite. Riprova più tardi.")
    
    async def show_balance(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Mostra solo il saldo corrente"""
        try:
            balance_data = self.calculate_balance()
            
            saldo = balance_data['saldo']
            saldo_icon = "🟢" if saldo >= 0 else "🔴"
            saldo_text = "POSITIVO" if saldo >= 0 else "NEGATIVO"
            
            balance_text = f"💰 **SALDO ATTUALE**\n\n"
            balance_text += f"💵 Totale puntato: €{balance_data['total_bet']:.2f}\n"
            balance_text += f"🏆 Totale vinto: €{balance_data['total_winnings']:.2f}\n"
            balance_text += f"{saldo_icon} **SALDO: €{saldo:.2f} ({saldo_text})**\n\n"
            balance_text += f"📊 Giocate: {balance_data['total_bets']} ({balance_data['wins']}W-{balance_data['losses']}L)"
            
            await update.message.reply_text(balance_text, parse_mode='Markdown')
            
        except Exception as e:
            logger.error(f"Errore show_balance: {e}")
            await update.message.reply_text("❌ Errore nel caricamento saldo. Riprova più tardi.")
    
    async def export_csv(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Esporta i dati in formato CSV"""
        try:
            superquotes = self.get_all_superquotes()
            
            if not superquotes:
                await update.message.reply_text("📊 Nessun dato da esportare!")
                return
            
            output = io.StringIO()
            writer = csv.writer(output)
            
            writer.writerow(['ID', 'Data', 'Risultato', 'Quota', 'Importo', 'Vincita', 'Esito', 'Registrato da'])
            
            for sq in superquotes:
                writer.writerow([
                    sq.get('quote_id', 'N/A'),
                    sq['data'],
                    sq['risultato'],
                    sq['quota'],
                    sq['importo'],
                    sq['vincita'],
                    sq['esito'],
                    sq.get('registrato_da', 'N/A')
                ])
            
            csv_data = output.getvalue().encode('utf-8')
            csv_filename = f'superquote_export_{datetime.now().strftime("%Y%m%d_%H%M%S")}.csv'
            
            balance_data = self.calculate_balance()
            saldo = balance_data['saldo']
            saldo_text = "POSITIVO" if saldo >= 0 else "NEGATIVO"
            
            await update.message.reply_document(
                document=io.BytesIO(csv_data),
                filename=csv_filename,
                caption=f"📊 Export completo delle superquote\n🎯 {len(superquotes)} record esportati\n💰 Saldo attuale: €{saldo:.2f} ({saldo_text})"
            )
            
        except Exception as e:
            logger.error(f"Errore durante l'export: {e}")
            await update.message.reply_text("❌ Errore durante l'export. Riprova più tardi.")
    
    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Mostra i comandi disponibili"""
        help_text = """
🤖 **BOT SUPERQUOTE CONDIVISE**

📝 **COME REGISTRARE:**
Scrivi: `SQ-risultato-quota-importo-esito`

🎯 **ESEMPI:**
• `SQ-1MILAN-2.00-10.00-VINTA`
• `SQ-OVER2.5-1.85-15.00-PERSA`
• `SQ-COMBO-3.20-5.00-VINTA`
• `SQ-GG-1.65-20.00-VINTA`

✏️ **COME MODIFICARE:**
Scrivi: `MODIFICA-ID-ESITO`

🔧 **ESEMPI MODIFICA:**
• `MODIFICA-A1B2C3D4-VINTA`
• `MODIFICA-E5F6G7H8-PERSA`

📊 **COMANDI:**
/stats - Statistiche complete con saldo
/lista - Ultime superquote con ID
/vincite - Solo le vincite recenti  
/saldo - Mostra solo il saldo attuale
/graph - Grafico andamento vincite
/export - Esporta tutto in CSV
/help - Questo messaggio

🎲 **ESITI VALIDI:**
VINTA, VINCITA, WIN → registra come vincita
PERSA, PERDITA, LOSS → registra come perdita

⚠️ **IMPORTANTE:**
- Il terzo numero è l'IMPORTO GIOCATO
- La vincita si calcola automaticamente (quota × importo)
- Ogni giocata ha un ID univoco per modifiche
- Il saldo mostra se sei in positivo o negativo

Il bot salva automaticamente tutto in MongoDB! 🗂️
        """
        await update.message.reply_text(help_text, parse_mode='Markdown')
    
    def run(self):
        """Avvia il bot"""
        try:
            application = Application.builder().token(self.token).build()
            
            application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_message))
            application.add_handler(CommandHandler("stats", self.show_stats))
            application.add_handler(CommandHandler("lista", self.show_list))
            application.add_handler(CommandHandler("vincite", self.show_recent_wins))
            application.add_handler(CommandHandler("saldo", self.show_balance))
            application.add_handler(CommandHandler("graph", self.generate_profit_graph))
            application.add_handler(CommandHandler("export", self.export_csv))
            application.add_handler(CommandHandler("help", self.help_command))
            application.add_handler(CommandHandler("start", self.help_command))
            
            logger.info("🤖 Bot Superquote Enhanced avviato con successo!")
            print("🤖 Bot Superquote Enhanced avviato! Premi Ctrl+C per fermare.")
            print("📊 Comandi disponibili: /stats, /lista, /vincite, /saldo, /graph, /export, /help")
            
            application.run_polling(allowed_updates=Update.ALL_TYPES)
            
        except Exception as e:
            logger.error(f"Errore nell'avvio del bot: {e}")
            raise

# Configurazione e avvio
if __name__ == "__main__":
    import os
    
    # Leggi le variabili d'ambiente
    BOT_TOKEN = os.getenv('BOT_TOKEN')
    MONGO_URI = os.getenv('MONGO_URL')
    
    if not BOT_TOKEN or not MONGO_URI:
        print("❌ ERRORE: Imposta le variabili d'ambiente BOT_TOKEN e MONGO_URI")
        print("💡 Su Railway: vai in Variables tab e aggiungi:")
        print("   BOT_TOKEN=il_tuo_token_di_telegram")
        print("   MONGO_URI=la_tua_stringa_di_connessione_mongodb")
        exit(1)
    
    try:
        bot = SuperquoteBot(BOT_TOKEN, MONGO_URI)
        bot.run()
    except ConnectionFailure as e:
        print(f"❌ ERRORE CONNESSIONE MONGODB: {e}")
        print("💡 Soluzioni:")
        print("1. Verifica che MongoDB sia attivo su Railway")
        print("2. Controlla la stringa di connessione MONGO_URI")
        print("3. Se hai esaurito lo spazio, usa MongoDB Atlas gratuito")
        exit(1)
    except Exception as e:
        print(f"❌ ERRORE IMPREVISTO: {e}")
        exit(1)