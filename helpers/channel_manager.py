import json
import os
from typing import Dict, Optional

class ChannelManager:
    def __init__(self):
        self.channel_file = "user_channels.json"
        self.user_channels: Dict[int, int] = {}
        self.load_user_channels()
    
    def load_user_channels(self):
        """Load user channels from file"""
        try:
            if os.path.exists(self.channel_file):
                with open(self.channel_file, 'r') as f:
                    data = json.load(f)
                    # Convert string keys back to int
                    self.user_channels = {int(k): v for k, v in data.get('user_channels', {}).items()}
        except Exception as e:
            print(f"Error loading user channels: {e}")
            self.user_channels = {}
    
    def save_user_channels(self):
        """Save user channels to file"""
        try:
            data = {
                'user_channels': {str(k): v for k, v in self.user_channels.items()}
            }
            with open(self.channel_file, 'w') as f:
                json.dump(data, f, indent=2)
            return True
        except Exception as e:
            print(f"Error saving user channels: {e}")
            return False
    
    def set_channel(self, user_id: int, channel_id: int) -> bool:
        """Set extraction channel for user"""
        self.user_channels[user_id] = channel_id
        return self.save_user_channels()
    
    def get_channel(self, user_id: int) -> Optional[int]:
        """Get extraction channel for user"""
        return self.user_channels.get(user_id)
    
    def reset_channel(self, user_id: int) -> bool:
        """Reset extraction channel for user (back to DM)"""
        if user_id in self.user_channels:
            del self.user_channels[user_id]
            return self.save_user_channels()
        return True
    
    def has_channel(self, user_id: int) -> bool:
        """Check if user has set a channel"""
        return user_id in self.user_channels
