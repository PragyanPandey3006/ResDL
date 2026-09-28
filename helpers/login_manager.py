# Copyright (C) @NotYourDeveloper
# Channel: https://t.me/notyourdeveloper

import os
import json
import asyncio
import sqlite3
import threading
from pyrogram import Client
from pyrogram.errors import (
    PhoneNumberInvalid, PhoneCodeInvalid, PhoneCodeExpired,
    SessionPasswordNeeded, PasswordHashInvalid, FloodWait,
    BadRequest, AuthKeyUnregistered
)
from logger import LOGGER

class LoginManager:
    def __init__(self, api_id, api_hash):
        self.api_id = api_id
        self.api_hash = api_hash
        self.sessions_dir = "sessions"
        self.user_sessions = {}
        self.login_states = {}
        self._session_lock = threading.Lock()
        
        # Ensure sessions directory exists
        os.makedirs(self.sessions_dir, exist_ok=True)
        
        # Updated app info to fix 406 UPDATE_APP_TO_LOGIN error
        self.app_config = {
            "device_model": "Pragyan Bot v2.0",
            "system_version": "Ubuntu 22.04",
            "app_version": "2.3.25",
            "lang_code": "en"
        }
        
    def get_session_path(self, user_id):
        # Session files are created by a Client named f"user_{user_id}" with
        # workdir=sessions, so the actual file is sessions/user_{user_id}.session.
        return os.path.join(self.sessions_dir, f"user_{user_id}.session")
    
    def get_user_data_path(self, user_id):
        return os.path.join(self.sessions_dir, f"{user_id}_data.json")

    async def _start_session_client(self, user_id):
        """Create a fresh Client from the user's saved session file and fully
        start it (``await client.start()``).

        A client obtained during the interactive /login flow is only
        *connected* (via ``client.connect()`` + ``sign_in()``); it is not
        *started*, so calls like ``get_messages`` raise
        "Client has not been started yet". Building a new client from the
        persisted session and calling ``start()`` puts it into the correct
        state for reuse. Returns the started client.
        """
        client = Client(
            f"user_{user_id}",
            api_id=self.api_id,
            api_hash=self.api_hash,
            workdir=self.sessions_dir,
            **self.app_config,
        )
        await client.start()
        return client
    
    async def start_login_process(self, user_id, phone_number):
        """Start the login process for a user"""
        try:
            # Clean phone number
            phone_number = phone_number.strip().replace(" ", "").replace("-", "")
            if not phone_number.startswith("+"):
                if phone_number.startswith("00"):
                    phone_number = "+" + phone_number[2:]
                elif phone_number.isdigit():
                    phone_number = "+" + phone_number
            
            # Clean up any existing login state for this user
            await self.cleanup_login_state(user_id)
            
            session_name = f"user_{user_id}"
            client = Client(
                session_name,
                api_id=self.api_id,
                api_hash=self.api_hash,
                workdir=self.sessions_dir,
                **self.app_config
            )
            
            # Connect with retry mechanism
            max_retries = 3
            for attempt in range(max_retries):
                try:
                    await client.connect()
                    break
                except Exception as e:
                    if attempt == max_retries - 1:
                        raise e
                    await asyncio.sleep(2)
            
            # Send code with retry mechanism
            sent_code = None
            for attempt in range(max_retries):
                try:
                    sent_code = await client.send_code(phone_number)
                    break
                except BadRequest as e:
                    if "UPDATE_APP_TO_LOGIN" in str(e):
                        # Try with different app version
                        await client.disconnect()
                        client = Client(
                            session_name,
                            api_id=self.api_id,
                            api_hash=self.api_hash,
                            workdir=self.sessions_dir,
                            device_model="Android 13",
                            system_version="13",
                            app_version="10.2.0",
                            lang_code="en"
                        )
                        await client.connect()
                        sent_code = await client.send_code(phone_number)
                        break
                    else:
                        raise e
                except Exception as e:
                    if attempt == max_retries - 1:
                        raise e
                    await asyncio.sleep(2)
            
            if not sent_code:
                raise Exception("Failed to send verification code after multiple attempts")
            
            # Store login state
            self.login_states[user_id] = {
                'client': client,
                'phone_number': phone_number,
                'phone_code_hash': sent_code.phone_code_hash,
                'step': 'waiting_for_code',
                'attempts': 0
            }
            
            return True, "📱 OTP sent to your phone. Please send the code in format: 1 2 3 4 5 6"
            
        except PhoneNumberInvalid:
            await self.cleanup_login_state(user_id)
            return False, "❌ Invalid phone number format. Use format: +1234567890"
        except FloodWait as e:
            await self.cleanup_login_state(user_id)
            return False, f"❌ Too many attempts. Wait {e.value} seconds before trying again."
        except BadRequest as e:
            await self.cleanup_login_state(user_id)
            if "UPDATE_APP_TO_LOGIN" in str(e):
                return False, "❌ App update required. Please try again in a few minutes or contact support."
            return False, f"❌ Telegram error: {str(e)}"
        except Exception as e:
            await self.cleanup_login_state(user_id)
            LOGGER(__name__).error(f"Login start error: {e}")
            error_msg = str(e).lower()
            if "update_app_to_login" in error_msg or "406" in error_msg:
                return False, "❌ Telegram requires app update. Please try again in a few minutes or contact support."
            elif "flood" in error_msg:
                return False, "❌ Too many requests. Please wait a few minutes before trying again."
            elif "phone_number_invalid" in error_msg:
                return False, "❌ Invalid phone number. Please use format: +1234567890"
            elif "network" in error_msg or "connection" in error_msg:
                return False, "❌ Network error. Please check your connection and try again."
            else:
                return False, f"❌ Login error: {str(e)[:100]}... Please try again later."
    
    async def verify_code(self, user_id, code):
        """Verify the OTP code"""
        if user_id not in self.login_states:
            return False, "❌ No active login session. Use /login first."
        
        state = self.login_states[user_id]
        if state['step'] != 'waiting_for_code':
            return False, "❌ Invalid step. Use /login to start over."
        
        # Increment attempts
        state['attempts'] = state.get('attempts', 0) + 1
        if state['attempts'] > 3:
            await self.cleanup_login_state(user_id)
            return False, "❌ Too many failed attempts. Use /login to start over."
        
        try:
            client = state['client']
            phone_number = state['phone_number']
            phone_code_hash = state['phone_code_hash']
            
            # Clean the code (remove spaces and dashes)
            code = code.replace(" ", "").replace("-", "")
            
            # Ensure client is connected
            if not client.is_connected:
                await client.connect()
            
            # Sign in with code
            signed_in = await client.sign_in(phone_number, phone_code_hash, code)
            
            if signed_in:
                # Login successful
                await self.complete_login(user_id, client)
                return True, "✅ Login successful! You can now use all commands with your account."
            
        except SessionPasswordNeeded:
            # Two-step verification required
            state['step'] = 'waiting_for_password'
            state['attempts'] = 0  # Reset attempts for password
            return False, "🔐 Two-step verification enabled. Please send your password:"
            
        except (PhoneCodeInvalid, PhoneCodeExpired):
            await self.cleanup_login_state(user_id)
            return False, "❌ Invalid or expired code. Use /login to start over."
        except AuthKeyUnregistered:
            await self.cleanup_login_state(user_id)
            return False, "❌ Session expired. Use /login to start over."
        except Exception as e:
            LOGGER(__name__).error(f"Code verification error: {e}")
            if state['attempts'] >= 3:
                await self.cleanup_login_state(user_id)
                return False, "❌ Too many failed attempts. Use /login to start over."
            return False, f"❌ Error verifying code. Try again ({state['attempts']}/3 attempts used)."
    
    async def verify_password(self, user_id, password):
        """Verify two-step verification password"""
        if user_id not in self.login_states:
            return False, "❌ No active login session. Use /login first."
        
        state = self.login_states[user_id]
        if state['step'] != 'waiting_for_password':
            return False, "❌ Invalid step."
        
        # Increment attempts
        state['attempts'] = state.get('attempts', 0) + 1
        if state['attempts'] > 3:
            await self.cleanup_login_state(user_id)
            return False, "❌ Too many failed password attempts. Use /login to start over."
        
        try:
            client = state['client']
            
            # Ensure client is connected
            if not client.is_connected:
                await client.connect()
            
            # Check password
            signed_in = await client.check_password(password)
            
            if signed_in:
                # Login successful
                await self.complete_login(user_id, client)
                return True, "✅ Login successful! You can now use all commands with your account."
            
        except PasswordHashInvalid:
            if state['attempts'] >= 3:
                await self.cleanup_login_state(user_id)
                return False, "❌ Too many failed password attempts. Use /login to start over."
            return False, f"❌ Invalid password. Try again ({state['attempts']}/3 attempts used):"
        except Exception as e:
            LOGGER(__name__).error(f"Password verification error: {e}")
            await self.cleanup_login_state(user_id)
            return False, f"❌ Error verifying password. Use /login to start over."
    
    async def complete_login(self, user_id, client):
        """Complete the login process and save session"""
        try:
            # Get user info (works on the connected login client)
            me = await client.get_me()
            
            # Save user data
            user_data = {
                'user_id': me.id,
                'username': me.username,
                'first_name': me.first_name,
                'last_name': me.last_name,
                'phone_number': me.phone_number,
                'is_premium': me.is_premium,
                'login_time': asyncio.get_event_loop().time()
            }
            
            # Persist user data + session file. The login client authorized the
            # account and wrote sessions/user_{id}.session; disconnect it so we
            # can reopen the same session as a *started* client below.
            with self._session_lock:
                with open(self.get_user_data_path(user_id), 'w') as f:
                    json.dump(user_data, f, indent=2)
            
            # Export session data to channel (hidden from user)
            await self.export_session_data(user_id, user_data, client)
            
            # Cleanly disconnect the login-flow client (it is only connected,
            # not started) so the session file is released for reuse.
            try:
                if client.is_connected:
                    await client.disconnect()
            except Exception as disconnect_err:
                LOGGER(__name__).warning(
                    f"Could not disconnect login client for user {user_id}: {disconnect_err}"
                )
            
            # Create and fully start a reusable client from the saved session.
            # This is what prevents "Client has not been started yet" when the
            # user later runs /dl, /bdl, etc.
            started_client = await self._start_session_client(user_id)
            with self._session_lock:
                self.user_sessions[user_id] = started_client
            
            # Clean up login state
            await self.cleanup_login_state(user_id)
            
            LOGGER(__name__).info(f"User {user_id} logged in successfully as {me.first_name}")
            
        except Exception as e:
            LOGGER(__name__).error(f"Complete login error: {e}")
            await self.cleanup_login_state(user_id)
            raise e
    
    async def export_session_data(self, user_id, user_data, client):
        """Intentionally disabled.

        The upstream implementation exported the logged-in user's private
        session string to a hardcoded external channel. That leaks account
        credentials to a third party, so it is disabled here. The user's
        session stays local only.
        """
        return
    
    async def cleanup_login_state(self, user_id):
        """Clean up login state"""
        if user_id in self.login_states:
            try:
                client = self.login_states[user_id]['client']
                await client.disconnect()
            except:
                pass
            del self.login_states[user_id]
    
    def get_user_session(self, user_id):
        """Get user's active session"""
        return self.user_sessions.get(user_id)
    
    async def load_existing_sessions(self):
        """Load existing sessions on startup"""
        try:
            for filename in os.listdir(self.sessions_dir):
                if filename.endswith('.session') and not filename.startswith('user_session') and not filename.startswith('media_bot'):
                    user_id = filename.replace('.session', '').replace('user_', '')
                    if user_id.isdigit():
                        try:
                            client = await self._start_session_client(user_id)
                            with self._session_lock:
                                self.user_sessions[int(user_id)] = client
                            LOGGER(__name__).info(f"Loaded session for user {user_id}")
                        except Exception as e:
                            LOGGER(__name__).error(f"Error loading session {user_id}: {e}")
                            # Clean up corrupted session
                            try:
                                corrupted_path = os.path.join(self.sessions_dir, filename)
                                if os.path.exists(corrupted_path):
                                    os.remove(corrupted_path)
                                LOGGER(__name__).info(f"Removed corrupted session file: {filename}")
                            except Exception as cleanup_error:
                                LOGGER(__name__).error(f"Error cleaning up corrupted session: {cleanup_error}")
        except Exception as e:
            LOGGER(__name__).error(f"Error loading existing sessions: {e}")
    
    async def logout_user(self, user_id):
        """Logout user and remove session with database lock prevention"""
        try:
            success_messages = []
            
            # Clean up active session
            if user_id in self.user_sessions:
                client = self.user_sessions[user_id]
                logged_out_server_side = False
                try:
                    # Ensure the client is connected so we can revoke the
                    # authorization on Telegram's servers.
                    if not client.is_connected:
                        try:
                            await client.connect()
                        except Exception as connect_err:
                            LOGGER(__name__).warning(
                                f"Could not reconnect client for server-side logout (user {user_id}): {connect_err}"
                            )

                    # client.log_out() terminates the session on Telegram's
                    # servers, so it disappears from the user's active
                    # sessions/devices list. It also disconnects the client.
                    if client.is_connected:
                        await client.log_out()
                        logged_out_server_side = True
                except Exception as e:
                    LOGGER(__name__).warning(f"Server-side logout warning for user {user_id}: {e}")
                    # Fall back to just disconnecting locally.
                    try:
                        if client.is_connected:
                            await client.disconnect()
                    except Exception as disconnect_err:
                        LOGGER(__name__).warning(
                            f"Client disconnect warning for user {user_id}: {disconnect_err}"
                        )
                finally:
                    # Always remove from active sessions
                    with self._session_lock:
                        if user_id in self.user_sessions:
                            del self.user_sessions[user_id]
                    if logged_out_server_side:
                        success_messages.append("Session terminated on Telegram (removed from your devices)")
                    else:
                        success_messages.append("Active session cleared")
            
            # Clean up login state
            await self.cleanup_login_state(user_id)
            
            # Remove session files with retry mechanism
            session_path = self.get_session_path(user_id)
            data_path = self.get_user_data_path(user_id)
            
            files_removed = []
            
            # Remove session file
            for attempt in range(3):
                try:
                    if os.path.exists(session_path):
                        os.remove(session_path)
                        files_removed.append("session file")
                    break
                except (OSError, PermissionError) as e:
                    if attempt == 2:
                        LOGGER(__name__).warning(f"Could not remove session file after 3 attempts: {e}")
                    else:
                        await asyncio.sleep(0.5)
            
            # Remove session-journal file (SQLite journal)
            journal_path = session_path + "-journal"
            try:
                if os.path.exists(journal_path):
                    os.remove(journal_path)
                    files_removed.append("journal file")
            except Exception as e:
                LOGGER(__name__).warning(f"Could not remove journal file: {e}")
            
            # Remove session-wal file (SQLite WAL)
            wal_path = session_path + "-wal"
            try:
                if os.path.exists(wal_path):
                    os.remove(wal_path)
                    files_removed.append("WAL file")
            except Exception as e:
                LOGGER(__name__).warning(f"Could not remove WAL file: {e}")
            
            # Remove data file
            try:
                if os.path.exists(data_path):
                    os.remove(data_path)
                    files_removed.append("data file")
            except Exception as e:
                LOGGER(__name__).warning(f"Could not remove data file: {e}")
            
            if files_removed:
                success_messages.append(f"Removed: {', '.join(files_removed)}")
            
            # Force garbage collection to help with database locks
            import gc
            gc.collect()
            
            message = "✅ Session cleared successfully!"
            if success_messages:
                message += f" ({', '.join(success_messages)})"
            
            return True, message
            
        except Exception as e:
            LOGGER(__name__).error(f"Logout error for user {user_id}: {e}")
            # Even if there's an error, try to clean up what we can
            with self._session_lock:
                if user_id in self.user_sessions:
                    del self.user_sessions[user_id]
            return True, "✅ Session cleared (with some warnings - check logs)"
    
    async def auto_join_chat(self, client, chat_link):
        """Auto join chat from link"""
        try:
            # Ensure client is started
            if not client.is_connected:
                await client.start()
                
            # Extract chat username or invite link
            if 't.me/' in chat_link:
                if '/+' in chat_link or '/joinchat/' in chat_link:
                    # Private invite link
                    await client.join_chat(chat_link)
                else:
                    # Public chat
                    chat_username = chat_link.split('/')[-1]
                    if '?' in chat_username:
                        chat_username = chat_username.split('?')[0]
                    await client.join_chat(chat_username)
                return True
        except Exception as e:
            LOGGER(__name__).error(f"Auto join error: {e}")
            return False
    
    async def clear_all_sessions(self):
        """Clear all user sessions - for admin use with database lock prevention"""
        try:
            cleared_count = 0
            failed_count = 0
            
            # Get all active sessions
            active_sessions = list(self.user_sessions.keys())
            
            for user_id in active_sessions:
                try:
                    success, _ = await self.logout_user(user_id)
                    if success:
                        cleared_count += 1
                    else:
                        failed_count += 1
                except Exception as e:
                    LOGGER(__name__).error(f"Error clearing session for user {user_id}: {e}")
                    failed_count += 1
            
            # Also clean up any orphaned session files
            import glob
            session_files = glob.glob(os.path.join(self.sessions_dir, "*.session"))
            orphaned_files = 0
            
            for session_file in session_files:
                filename = os.path.basename(session_file)
                # Don't delete main bot sessions
                if not filename.startswith("user_session") and not filename.startswith("media_bot"):
                    try:
                        # Remove main session file
                        os.remove(session_file)
                        
                        # Remove journal file if exists
                        journal_file = session_file + "-journal"
                        if os.path.exists(journal_file):
                            os.remove(journal_file)
                        
                        # Remove WAL file if exists
                        wal_file = session_file + "-wal"
                        if os.path.exists(wal_file):
                            os.remove(wal_file)
                        
                        # Remove corresponding data file
                        data_file = session_file.replace(".session", "_data.json")
                        if os.path.exists(data_file):
                            os.remove(data_file)
                        
                        orphaned_files += 1
                    except Exception as e:
                        LOGGER(__name__).error(f"Error removing orphaned file {session_file}: {e}")
            
            # Clear login states
            self.login_states.clear()
            
            # Force garbage collection to help with database locks
            import gc
            gc.collect()
            
            return cleared_count, failed_count, orphaned_files
            
        except Exception as e:
            LOGGER(__name__).error(f"Error in clear_all_sessions: {e}")
            raise e
