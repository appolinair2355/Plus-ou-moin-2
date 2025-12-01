import os
import yaml
from typing import Any, Optional

class YamlDatabase:
    """Simple YAML-based database for storing bot configuration and data"""
    
    def __init__(self, db_file: str = "bot_data.yaml"):
        self.db_file = db_file
        self.data = {}
        self.load_data()
    
    def load_data(self):
        """Load data from YAML file"""
        try:
            if os.path.exists(self.db_file):
                with open(self.db_file, 'r', encoding='utf-8') as f:
                    self.data = yaml.safe_load(f) or {}
                print(f"✅ Base de données chargée: {len(self.data)} entrées")
            else:
                self.data = {}
                print("ℹ️ Aucune base de données existante, création d'une nouvelle")
        except Exception as e:
            print(f"❌ Erreur chargement base de données: {e}")
            self.data = {}
    
    def save_data(self):
        """Save data to YAML file"""
        try:
            with open(self.db_file, 'w', encoding='utf-8') as f:
                yaml.dump(self.data, f, allow_unicode=True, default_flow_style=False)
            print(f"💾 Base de données sauvegardée: {len(self.data)} entrées")
        except Exception as e:
            print(f"❌ Erreur sauvegarde base de données: {e}")
    
    def get_config(self, key: str) -> Optional[Any]:
        """Get configuration value"""
        return self.data.get('config', {}).get(key)
    
    def set_config(self, key: str, value: Any):
        """Set configuration value"""
        if 'config' not in self.data:
            self.data['config'] = {}
        self.data['config'][key] = value
        self.save_data()
    
    def reset_all_data(self):
        """Reset all data in the database"""
        self.data = {}
        self.save_data()
        print("🗑️ Base de données réinitialisée")

# Global database instance
db = None

def init_database(db_file: str = "bot_data.yaml") -> YamlDatabase:
    """Initialize the global database instance"""
    global db
    db = YamlDatabase(db_file)
    return db
