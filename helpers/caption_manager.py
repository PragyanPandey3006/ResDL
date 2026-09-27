# Copyright (C) @NotYourDeveloper
# Channel: https://t.me/notyourdeveloper

import os
import json
import re
from typing import Optional, Dict

class CaptionManager:
    def __init__(self, storage_file: str = "user_captions.json"):
        self.storage_file = storage_file
        self.user_captions: Dict[int, str] = {}
        self.load_captions()
    
    def load_captions(self):
        """Load user captions from storage file"""
        try:
            if os.path.exists(self.storage_file):
                with open(self.storage_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    # Convert string keys back to int
                    self.user_captions = {int(k): v for k, v in data.items()}
        except Exception as e:
            print(f"Error loading captions: {e}")
            self.user_captions = {}
    
    def save_captions(self):
        """Save user captions to storage file"""
        try:
            with open(self.storage_file, 'w', encoding='utf-8') as f:
                # Convert int keys to string for JSON
                data = {str(k): v for k, v in self.user_captions.items()}
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"Error saving captions: {e}")
    
    def set_caption(self, user_id: int, caption: str) -> bool:
        """Set caption for a user"""
        try:
            self.user_captions[user_id] = caption
            self.save_captions()
            return True
        except Exception:
            return False
    
    def get_caption(self, user_id: int) -> Optional[str]:
        """Get caption for a user"""
        return self.user_captions.get(user_id)
    
    def remove_caption(self, user_id: int) -> bool:
        """Remove caption for a user"""
        try:
            if user_id in self.user_captions:
                del self.user_captions[user_id]
                self.save_captions()
                return True
            return False
        except Exception:
            return False
    
    def has_caption(self, user_id: int) -> bool:
        """Check if user has a caption set"""
        return user_id in self.user_captions
    
    def apply_caption_to_filename(self, original_filename: str, caption: str) -> str:
        """Apply caption to filename while preserving extension"""
        try:
            # Get file extension
            name, ext = os.path.splitext(original_filename)
            
            # Clean caption for filename (remove invalid characters)
            clean_caption = re.sub(r'[<>:"/\\|?*]', '_', caption)
            clean_caption = clean_caption.strip()
            
            # Limit filename length
            if len(clean_caption) > 100:
                clean_caption = clean_caption[:100]
            
            # Create new filename
            new_filename = f"{clean_caption}{ext}"
            return new_filename
        except Exception:
            return original_filename
    
    def remove_caption_from_filename(self, filename: str, caption_to_remove: str) -> str:
        """Remove specific caption from filename"""
        try:
            # Get file extension
            name, ext = os.path.splitext(filename)
            
            # Clean the caption that needs to be removed
            clean_caption = re.sub(r'[<>:"/\\|?*]', '_', caption_to_remove)
            clean_caption = clean_caption.strip()
            
            # Remove the caption from the filename
            if name.startswith(clean_caption):
                # Remove caption and any following underscore or space
                remaining = name[len(clean_caption):].lstrip('_').lstrip()
                new_filename = f"{remaining}{ext}" if remaining else f"file{ext}"
            else:
                new_filename = filename
            
            return new_filename
        except Exception:
            return filename
