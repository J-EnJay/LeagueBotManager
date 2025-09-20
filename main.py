import base64
import json
import random
import re
import subprocess
import sys
import time
from PyQt5.QtCore import Qt, QThread, pyqtSignal, QByteArray, QTimer
from PyQt5.QtGui import QFont, QIcon, QPixmap
from PyQt5.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
                             QLineEdit, QPushButton, QComboBox, QLabel, QGroupBox, QGridLayout,
                             QFrame)

# 禁用SSL警告
import requests
import urllib3
from urllib3.exceptions import InsecureRequestWarning

urllib3.disable_warnings(InsecureRequestWarning)


class ConnectionChecker(QThread):
    """连接检查线程"""
    connection_update = pyqtSignal(bool, str, str)

    def __init__(self):
        super().__init__()
        self.running = True
        self.wait_time = 2  # 检查间隔时间（秒）

    def run(self):
        while self.running:
            # 使用小间隔循环检查running状态，提高终止响应速度
            for _ in range(self.wait_time * 10):  # 10个0.1秒间隔
                if not self.running:
                    return
                time.sleep(0.1)

            port, token = get_lcu_credentials()
            if port and token:
                client = LCUClient(port, token)
                game_state = client.get_gameflow_phase()
                status_text = f"已连接 (端口: {port})"
                if game_state:
                    status_text += f" | 游戏状态: {game_state}"
                self.connection_update.emit(True, status_text, port)
            else:
                self.connection_update.emit(False, "未检测到英雄联盟客户端\n请确保以管理员身份运行", "")

    def stop(self):
        """立即停止线程"""
        self.running = False
        # 等待线程结束，最多等待1秒
        if not self.wait(1000):
            self.terminate()  # 强制终止
            self.wait()


class WorkerThread(QThread):
    """工作线程，用于执行耗时操作"""
    finished = pyqtSignal(bool, str)
    progress = pyqtSignal(str)

    def __init__(self, client, champions_data, room_name, room_password, selected_team):
        super().__init__()
        self.client = client
        self.champions_data = champions_data
        self.room_name = room_name
        self.room_password = room_password
        self.selected_team = selected_team
        self.abort = False  # 用于标记是否需要中止任务

    def run(self):
        try:
            self.progress.emit("正在创建自定义房间...")
            if self.abort:
                self.finished.emit(False, "操作已取消")
                return

            if not self.client.create_custom_lobby(lobby_name=self.room_name, password=self.room_password):
                self.finished.emit(False, "创建自定义房间失败！")
                return

            # 等待时检查是否需要中止
            for _ in range(30):  # 3秒 = 30*0.1秒
                if self.abort:
                    self.finished.emit(False, "操作已取消")
                    return
                time.sleep(0.1)
            self.progress.emit("等待房间创建完成...")

            if not self.client.is_in_lobby():
                self.finished.emit(False, "未能成功进入自定义房间！")
                return

            self.progress.emit("正在清除现有人机...")
            if self.abort:
                self.finished.emit(False, "操作已取消")
                return
            self.client.clear_all_bots()

            self.progress.emit("正在添加AI英雄...")
            if self.abort:
                self.finished.emit(False, "操作已取消")
                return
            results = self.add_team_to_game(self.selected_team)

            success_count = sum(1 for result in results.values() if result['success'])
            if success_count == 5:
                self.finished.emit(True, "🎉 成功添加5个AI英雄！")
            else:
                self.finished.emit(True, f"⚠️ 成功添加 {success_count}/5 个AI英雄")

        except Exception as e:
            self.finished.emit(False, f"执行过程中发生错误: {str(e)}")

    def add_team_to_game(self, team, difficulty="RSINTERMEDIATE", team_id="200"):
        results = {}
        for position, champ_info in team.items():
            if self.abort:  # 检查是否需要中止
                return results

            bot_data = {
                "championId": champ_info['champion_id'],
                "botDifficulty": difficulty.upper(),
                "teamId": team_id,
                "position": position.upper()
            }

            success = self.client.add_bot(bot_data)
            results[position] = {
                'success': success,
                'champion': champ_info['name'],
                'champion_id': champ_info['champion_id']
            }

            self.progress.emit(f"{'✅' if success else '❌'} 添加 {position}: {champ_info['name']}")

            # 等待时检查是否需要中止
            for _ in range(5):  # 0.5秒 = 5*0.1秒
                if self.abort:
                    return results
                time.sleep(0.1)
            self.progress.emit("等待添加下一个英雄...")

        return results

    def stop(self):
        """中止当前任务"""
        self.abort = True


def load_champions_data(filename="ai_champions_data.json"):
    """加载英雄数据JSON文件"""
    try:
        with open(filename, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return {k: v for k, v in data.items() if v.get('enable', 1) == 1}
    except FileNotFoundError:
        print(f"❌ 文件 {filename} 不存在")
        return None
    except Exception as e:
        print(f"❌ 加载文件时出错: {e}")
        return None


def get_lcu_credentials():
    """获取LCU连接凭证"""
    try:
        output = subprocess.check_output(
            'wmic process where "name=\'LeagueClientUx.exe\'" get commandline',
            shell=True
        ).decode('gbk', errors='ignore')

        port_match = re.search(r'--app-port=(\d+)', output)
        token_match = re.search(r'--remoting-auth-token=([\w\-]+)', output)

        if port_match and token_match:
            port = port_match.group(1)
            token = token_match.group(1)
            auth_token = base64.b64encode(f"riot:{token}".encode()).decode()
            return port, auth_token
        return None, None

    except subprocess.CalledProcessError:
        print("❌ 请确保League客户端正在运行")
        return None, None
    except Exception as e:
        print(f"❌ 获取凭证时发生错误: {e}")
        return None, None


class LCUClient:
    def __init__(self, port, token):
        self.port = port
        self.base_url = f"https://127.0.0.1:{port}"
        self.headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Basic {token}"
        }

    def get_gameflow_phase(self):
        response = self._make_request('/lol-gameflow/v1/gameflow-phase')
        return response.text.strip('"') if response and response.status_code == 200 else None

    def get_custom_bots(self):
        response = self._make_request('/lol-lobby/v2/lobby')
        if response and response.status_code == 200:
            return [m for m in response.json().get('members', []) if m.get('isBot', False)]
        return []

    def add_bot(self, bot_data):
        for endpoint in ['/lol-lobby/v1/lobby/custom/bots', '/lol-lobby/v2/lobby/custom/bots']:
            response = self._make_request(endpoint, 'POST', bot_data)
            if response and response.status_code in [200, 201, 204]:
                return True
        return False

    def remove_bot(self, champion_id):
        for endpoint in [f'/lol-lobby/v1/lobby/custom/bots/{champion_id}',
                         f'/lol-lobby/v2/lobby/custom/bots/{champion_id}']:
            response = self._make_request(endpoint, 'DELETE')
            if response and response.status_code in [200, 204]:
                return True
        return False

    def clear_all_bots(self):
        success = True
        for bot in self.get_custom_bots():
            if bot.get('championId') and not self.remove_bot(bot['championId']):
                success = False
            time.sleep(0.2)
        return success

    def create_custom_lobby(self, map_id=11, mode="CLASSIC", lobby_name="AI Game", password=""):
        lobby_data = {
            "customGameLobby": {
                "configuration": {
                    "gameMode": mode,
                    "gameMutator": "",
                    "gameServerRegion": "",
                    "mapId": map_id,
                    "mutators": {"id": 1},
                    "spectatorPolicy": "AllAllowed",
                    "teamSize": 5
                },
                "lobbyName": lobby_name,
                "lobbyPassword": password
            },
            "isCustom": True
        }

        response = self._make_request('/lol-lobby/v2/lobby', 'POST', lobby_data)
        return response and response.status_code == 200

    def is_in_lobby(self):
        response = self._make_request('/lol-lobby/v2/lobby')
        return response and response.status_code == 200

    def _make_request(self, endpoint, method='GET', data=None):
        url = f"{self.base_url}{endpoint}"
        try:
            if method == 'GET':
                return requests.get(url, headers=self.headers, verify=False, timeout=5)
            elif method == 'POST':
                return requests.post(url, headers=self.headers, json=data, verify=False, timeout=5)
            elif method == 'DELETE':
                return requests.delete(url, headers=self.headers, verify=False, timeout=5)
        except Exception as e:
            print(f"请求错误 {endpoint}: {e}")
        return None


def get_champions_by_position(champions_data, position):
    """根据位置筛选英雄"""
    return {k: v for k, v in champions_data.items() if position in v.get('positions', [])}


def select_random_team(champions_data):
    """为五个位置随机选择英雄"""
    positions = ["TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY"]
    selected_team = {}

    for position in positions:
        position_champs = get_champions_by_position(champions_data, position)
        if not position_champs:
            print(f"❌ 没有找到适合 {position} 位置的英雄")
            continue

        used_ids = [str(m['champion_id']) for m in selected_team.values()]
        available = {k: v for k, v in position_champs.items() if k not in used_ids} or position_champs

        champ_id = random.choice(list(available.keys()))
        champ_data = available[champ_id]

        selected_team[position] = {
            'champion_id': int(champ_id),
            'name': champ_data['name'],
            'alias': champ_data['alias'],
            'primary_position': position
        }

    return selected_team


class AIBotManagerUI(QMainWindow):
    def __init__(self):
        super().__init__()
        self.champions_data = None
        self.selected_team = {}
        self.worker_thread = None
        self.client = None
        self.connection_checker = None
        self.current_port = ""
        self.dragging = False
        self.offset = None
        self.team_generated = False  # 跟踪是否已经自动生成过队伍
        self.is_executing = False  # 跟踪是否正在执行创建AI的操作
        # 初始化预设数据，包含5个预设，默认为空
        self.presets = [None] * 5  # 存储5个预设
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Window)
        self.init_ui()
        self.start_connection_check()
        self.load_champions_data()
        self.set_application_icon_from_base64()
        
        # 加载预设文件
        self.load_presets_from_file()
    
    def load_presets_from_file(self):
        """从文件加载预设列表"""
        try:
            with open('presets', 'r', encoding='utf-8') as f:
                serializable_presets = json.load(f)
                # 确保预设数量为5个
                if len(serializable_presets) >= 5:
                    self.presets = [None] * 5  # 重置预设数组
                    # 从索引1开始加载，保留索引0（无预设）为空
                    for i in range(1, 5):
                        preset_data = serializable_presets[i]
                        if preset_data is not None and self.champions_data:
                            # 还原预设数据为完整格式
                            full_preset = {}
                            for position, champ_info in preset_data.items():
                                champ_id = champ_info['champion_id']
                                # 检查英雄数据是否存在
                                if str(champ_id) in self.champions_data:
                                    full_preset[position] = {
                                        'champion_id': champ_id,
                                        'name': champ_info['name'],
                                        'alias': self.champions_data[str(champ_id)].get('alias', ''),
                                        'primary_position': position
                                    }
                            if full_preset:  # 只有当预设包含有效英雄时才保存
                                self.presets[i] = full_preset
        except FileNotFoundError:
            # 文件不存在，这是首次运行
            pass
        except Exception as e:
            print(f"加载预设失败: {e}")

    def set_application_icon_from_base64(self):
        """从base64编码数据设置应用程序图标"""
        try:
            icon_base64 = "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"
            icon_data = QByteArray.fromBase64(icon_base64.encode())
            pixmap = QPixmap()
            pixmap.loadFromData(icon_data)
            self.setWindowIcon(QIcon(pixmap))
        except Exception as e:
            print(f"❌ 从base64加载图标失败: {e}")

    def init_ui(self):
        # 样式表保持不变
        self.setStyleSheet("""
            * { font-family: 'Microsoft YaHei'; font-weight: bold; color: #e0e0e0; }
            QMainWindow { background: qlineargradient(x1:0,y1:0,x2:0,y2:1,stop:0 #121212,stop:1 #1a1a1a); }
            #titleBar { background: qlineargradient(x1:0,y1:0,x2:0,y2:1,stop:0 #1e1e1e,stop:1 #2d2d2d); 
                       border-bottom:1px solid #424242; border-radius:8px 8px 0 0; padding:5px; }
            #titleButton { font-family:'SimHei'; background:transparent; border:none; color:#e0e0e0; 
                         border-radius:3px; font-size:16px; min-width:25px; max-width:25px; 
                         min-height:25px; max-height:25px; }
            #titleButton:hover { background:#424242; }
            #closeButton:hover { background:#ff5252; color:white; }
            QGroupBox { font-size:24px; border:2px solid #2d2d2d; border-radius:8px; 
                       margin-top:1ex; padding-top:15px; background:#1e1e1e; }
            QPushButton { background-color:#2d2d2d; border:2px solid #424242; color:white; 
                        padding:12px 20px; border-radius:6px; font-size:20px; min-width:120px; }
            QPushButton:hover { border:2px solid #bb86fc; background:#221d29; }
            QPushButton:pressed { border:2px solid #bb86fc; background:#393340; }
            QPushButton:disabled { background:#424242; color:#757575; border:1px solid #616161; }
            QLineEdit { background-color:#2d2d2d; border:2px solid #424242; border-radius:4px; 
                      padding:8px; color:#e0e0e0; font-size:20px; 
                      selection-background-color:#6200ea; selection-color:white; }
            QLineEdit:focus { border-color:#bb86fc; background-color:#333333; }
            QLineEdit:disabled { background-color:#2d2d2d; color:#757575; }
            QComboBox { background-color:#2d2d2d; border:2px solid #424242; border-radius:4px; 
                      padding:8px; color:#e0e0e0; font-size:18px; min-width:150px; }
            QComboBox:focus { border-color:#bb86fc; }
            QComboBox:disabled { background-color:#2d2d2d; color:#757575; }
            QComboBox::drop-down { subcontrol-origin:padding; subcontrol-position:top right; 
                                 width:20px; border-left:1px solid #424242; 
                                 border-radius:0 4px 4px 0; background:#424242; }
            QComboBox::down-arrow { image:none; border-left:4px solid transparent; 
                                  border-right:4px solid transparent; border-top:6px solid #e0e0e0; }
            QComboBox QAbstractItemView { background-color:#2d2d2d; border:2px solid #424242; 
                                        border-radius:4px; selection-background-color:#6200ea; 
                                        selection-color:white; color:#e0e0e0; outline:0; }
            QComboBox QAbstractItemView::item { padding:5px; }
            QComboBox QAbstractItemView::item:selected { background-color:#6200ea; }
            QLabel { color:#e0e0e0; font-size:18px; padding:2px; }
            QLabel#statusLabel { font-size:18px; color:#ffd740; }
            QLabel#progressLabel { font-size:16px; color:#b0b0b0; font-style:italic; }
            QLabel#footerLabel { color:#757575; font-size:10px; }
            QFrame#statusFrame { background:rgba(45,45,45,0.5); border-radius:6px; border:1px solid #424242; }
            QScrollBar:vertical { border:none; background:#2d2d2d; width:10px; margin:0; }
            QScrollBar::handle:vertical { background:#424242; border-radius:5px; min-height:20px; }
            QScrollBar::handle:vertical:hover { background:#616161; }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height:0; }
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background:none; }
        """)

        # 窗口设置
        screen_rect = QApplication.primaryScreen().availableGeometry()
        window_width, window_height = 450, 600
        x = (screen_rect.width() - window_width) // 2
        y = (screen_rect.height() - window_height) // 2
        self.setGeometry(x, y, window_width, window_height)
        self.setWindowTitle("🎮 自定义AI练功房")

        # 主布局
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)
        main_layout.setSpacing(0)
        main_layout.setContentsMargins(0, 0, 0, 0)

        # 标题栏
        title_bar = QWidget()
        title_bar.setObjectName("titleBar")
        title_bar.setFixedHeight(35)
        title_layout = QHBoxLayout(title_bar)
        title_layout.setContentsMargins(10, 0, 10, 0)
        title_layout.setSpacing(5)

        title_label = QLabel("🎮 LOL自定义AI练功房")
        title_label.setStyleSheet("color: #bb86fc; font-weight: bold; font-size: 14px;")
        title_layout.addWidget(title_label)
        title_layout.addStretch()

        self.minimize_btn = QPushButton("—")
        self.minimize_btn.setObjectName("titleButton")
        self.minimize_btn.clicked.connect(self.showMinimized)
        title_layout.addWidget(self.minimize_btn)

        self.close_btn = QPushButton("×")
        self.close_btn.setObjectName("titleButton")
        self.close_btn.setProperty("id", "closeButton")
        self.close_btn.clicked.connect(self.close)
        title_layout.addWidget(self.close_btn)

        main_layout.addWidget(title_bar)

        # 内容区域（保持不变）
        content_widget = QWidget()
        content_layout = QVBoxLayout(content_widget)
        content_layout.setSpacing(15)
        content_layout.setContentsMargins(20, 15, 20, 20)

        # 连接状态组
        connection_group = QGroupBox("📡 客户端连接状态")
        connection_layout = QVBoxLayout()
        self.connection_status = QLabel("正在检测英雄联盟客户端...")
        self.connection_status.setAlignment(Qt.AlignCenter)
        self.connection_status.setFont(QFont("Segoe UI", 11, QFont.Bold))
        connection_layout.addWidget(self.connection_status)
        connection_group.setLayout(connection_layout)
        content_layout.addWidget(connection_group)

        # 房间信息组
        room_group = QGroupBox("🏠 房间设置")
        room_layout = QGridLayout()
        room_layout.setSpacing(10)
        room_layout.setContentsMargins(10, 15, 10, 15)
        room_layout.addWidget(QLabel("房间名称:"), 0, 0)
        self.room_name_input = QLineEdit("AI练功房")
        room_layout.addWidget(self.room_name_input, 0, 1)
        room_layout.addWidget(QLabel("房间密码:"), 1, 0)
        self.room_password_input = QLineEdit("123")
        self.room_password_input.setEchoMode(QLineEdit.Normal)
        room_layout.addWidget(self.room_password_input, 1, 1)
        room_group.setLayout(room_layout)
        content_layout.addWidget(room_group)

        # 按钮布局
        button_layout = QHBoxLayout()
        button_layout.setSpacing(15)
        self.generate_btn = QPushButton("🎲 随机AI队伍")
        self.generate_btn.clicked.connect(self.generate_team)
        button_layout.addWidget(self.generate_btn)
        self.execute_btn = QPushButton("🚀 执行创建")
        self.execute_btn.clicked.connect(self.execute)
        button_layout.addWidget(self.execute_btn)
        content_layout.addLayout(button_layout)

        # 英雄选择组
        self.hero_group = QGroupBox("⚔️ AI队伍")
        hero_layout = QGridLayout()
        hero_layout.setSpacing(8)
        hero_layout.setContentsMargins(10, 15, 10, 15)

        positions = ["TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY"]
        position_icons = {"TOP": "🛡️", "JUNGLE": "🌲", "MIDDLE": "🔥", "BOTTOM": "🏹", "UTILITY": "💫"}
        position_names = {"TOP": "上单", "JUNGLE": "打野", "MIDDLE": "中单", "BOTTOM": "ADC", "UTILITY": "辅助"}

        self.position_comboboxes = {}
        for i, position in enumerate(positions):
            position_label = QLabel(f"{position_icons[position]} {position_names[position]}")
            position_label.setFont(QFont("Segoe UI", 10, QFont.Bold))
            hero_layout.addWidget(position_label, i, 0)
            combo = QComboBox()
            combo.setMinimumWidth(180)
            self.position_comboboxes[position] = combo
            hero_layout.addWidget(combo, i, 1)

        # 添加预设功能
        preset_layout = QHBoxLayout()
        preset_label = QLabel("💾 预设:")
        preset_label.setFont(QFont("Segoe UI", 10, QFont.Bold))
        self.preset_combobox = QComboBox()
        self.preset_combobox.addItems(["无预设", "预设 1", "预设 2", "预设 3", "预设 4"])
        self.preset_combobox.currentIndexChanged.connect(self.on_preset_changed)
        self.save_preset_btn = QPushButton("保存预设")
        self.save_preset_btn.setEnabled(False)
        self.save_preset_btn.clicked.connect(self.save_preset)
        preset_layout.addWidget(preset_label)
        preset_layout.addWidget(self.preset_combobox)
        preset_layout.addWidget(self.save_preset_btn)
        
        # 初始化时检查预设选项状态
        self._update_save_preset_button_state()
        hero_layout.addLayout(preset_layout, 5, 0, 1, 2)  # 添加到英雄布局的最后一行

        self.hero_group.setLayout(hero_layout)
        content_layout.addWidget(self.hero_group)

        # 状态区域
        status_frame = QFrame()
        status_frame.setObjectName("statusFrame")
        status_layout = QVBoxLayout(status_frame)
        status_layout.setContentsMargins(15, 10, 15, 10)
        self.status_label = QLabel("就绪")
        self.status_label.setAlignment(Qt.AlignCenter)
        self.status_label.setObjectName("statusLabel")
        status_layout.addWidget(self.status_label)
        self.progress_label = QLabel("")
        self.progress_label.setAlignment(Qt.AlignCenter)
        self.progress_label.setObjectName("progressLabel")
        status_layout.addWidget(self.progress_label)
        content_layout.addWidget(status_frame)

        # 底部信息
        footer_label = QLabel("© 2025 LOL自定义AI练功房 | JEnJay")
        footer_label.setAlignment(Qt.AlignCenter)
        footer_label.setObjectName("footerLabel")
        content_layout.addWidget(footer_label)

        main_layout.addWidget(content_widget)
        self.set_ui_enabled(False)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.dragging = True
            self.offset = event.globalPos() - self.pos()
            event.accept()

    def mouseMoveEvent(self, event):
        if self.dragging and self.offset is not None:
            self.move(event.globalPos() - self.offset)
            event.accept()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.dragging = False
            self.offset = None
            event.accept()

    def load_champions_data(self):
        self.champions_data = load_champions_data()
        if not self.champions_data:
            self.status_label.setText("❌ 加载英雄数据失败")

    def start_connection_check(self):
        self.connection_checker = ConnectionChecker()
        self.connection_checker.connection_update.connect(self.update_connection_status)
        self.connection_checker.start()

    def update_connection_status(self, connected, status_text, port):
        if connected:
            self.connection_status.setStyleSheet("color: #00e676; font-weight: bold;")
            self.connection_status.setText("✅ " + status_text)
            self.current_port = port
            # 只有在不是执行状态时才启用UI
            if not self.is_executing:
                self.set_ui_enabled(True)
            self.status_label.setText("就绪")
            self.status_label.setStyleSheet("color: #00e676; font-weight: bold;")
            
            # 连接成功后自动生成队伍，但仅在程序开始时运行一次
            if self.champions_data and not self.team_generated:
                self.generate_team()
                self.team_generated = True
        else:
            self.connection_status.setStyleSheet("color: #ffab40; font-weight: bold;")
            self.connection_status.setText("❌ " + status_text)
            self.set_ui_enabled(False)
            self.status_label.setText("等待客户端连接...")
            self.status_label.setStyleSheet("color: #ffab40; font-weight: bold;")

    def generate_team(self):
        if not self.champions_data:
            self.status_label.setText("❌ 英雄数据未加载")
            return

        self.status_label.setText("正在生成队伍...")
        self.progress_label.setText("")
        QApplication.processEvents()

        self.selected_team = select_random_team(self.champions_data)

        for position, combo in self.position_comboboxes.items():
            combo.clear()
            position_champs = get_champions_by_position(self.champions_data, position)

            for champ_id, champ_data in position_champs.items():
                combo.addItem(champ_data['name'], champ_id)

            if position in self.selected_team:
                current_champ_id = self.selected_team[position]['champion_id']
                index = combo.findData(current_champ_id)
                if index >= 0:
                    combo.setCurrentIndex(index)

        self.hero_group.setEnabled(True)
        self.execute_btn.setEnabled(True)
        self.status_label.setText("队伍生成完成！")
        self.progress_label.setText("")

    def execute(self):
        if self.worker_thread and self.worker_thread.isRunning():
            return

        # 创建客户端连接
        if not self.client:
            port, token = get_lcu_credentials()
            if port and token:
                self.client = LCUClient(port, token)
            else:
                self.status_label.setText("❌ 无法创建客户端连接")
                return

        room_name = self.room_name_input.text()
        room_password = self.room_password_input.text()

        selected_team = {}
        for position, combo in self.position_comboboxes.items():
            champ_id = combo.currentData()
            if champ_id:
                champ_data = self.champions_data.get(str(champ_id))
                if champ_data:
                    selected_team[position] = {
                        'champion_id': int(champ_id),
                        'name': champ_data['name'],
                        'alias': champ_data['alias'],
                        'primary_position': position
                    }

        if len(selected_team) != 5:
            self.status_label.setText("错误：请为所有位置选择英雄！")
            return

        # 在执行创建过程中禁用按钮，并保存原始文本
        original_text = self.execute_btn.text()
        self.execute_btn.setEnabled(False)
        self.execute_btn.setText("🚀 执行中...")
        
        # 设置执行标志为True，防止update_connection_status覆盖UI状态
        self.is_executing = True
        
        # 禁用其他UI元素
        self.set_ui_enabled(False)
        
        # 确保执行按钮保持禁用状态（因为set_ui_enabled可能会尝试启用它）
        self.execute_btn.setEnabled(False)
        self.execute_btn.setText("🚀 执行中...")
        
        self.status_label.setText("开始执行...")
        self.progress_label.setText("")

        self.worker_thread = WorkerThread(
            self.client, self.champions_data, room_name, room_password, selected_team
        )
        self.worker_thread.progress.connect(self.update_progress)
        self.worker_thread.finished.connect(lambda success, message: self.on_execution_finished(success, message, original_text))
        self.worker_thread.start()

    def update_progress(self, message):
        self.progress_label.setText(message)
        QApplication.processEvents()

    def on_execution_finished(self, success, message, original_text=None):
        # 先设置状态信息，确保用户了解当前进度
        self.status_label.setText(message)
        
        # 根据执行结果显示相应的进度信息
        if success:
            self.progress_label.setText("操作完成，开始练功！")
            
            # 确保在所有英雄添加到房间完成之后再恢复按钮状态
            # 这里使用延迟恢复，给予系统足够时间完成英雄添加
            QTimer.singleShot(1000, lambda: self._restore_ui_state(original_text))
        else:
            self.progress_label.setText("操作失败，请检查客户端状态")
            # 失败情况下立即恢复UI状态，方便用户重试
            self._restore_ui_state(original_text)
            
        self.worker_thread = None  # 清除线程引用
        
    def _restore_ui_state(self, original_text=None):
        """恢复UI状态的辅助方法，确保在所有英雄添加完成后调用"""
        if original_text:
            self.execute_btn.setText(original_text)
        
        # 恢复UI状态并重置执行标志
        self.is_executing = False
        self.set_ui_enabled(True)
        
    def on_preset_changed(self, index):
        """切换预设时的回调函数"""
        # 更新保存预设按钮状态
        self._update_save_preset_button_state()
        
        if not self.champions_data:
            return
        
        preset = self.presets[index]
        if preset is None:
            # 预设为空，不执行任何操作
            return
        
        self.selected_team = preset.copy()  # 复制预设数据
        
        # 更新英雄选择下拉框
        for position, combo in self.position_comboboxes.items():
            if position in self.selected_team:
                current_champ_id = self.selected_team[position]['champion_id']
                index = combo.findData(current_champ_id)
                if index >= 0:
                    combo.setCurrentIndex(index)
        
        self.status_label.setText(f"已加载预设 {self.preset_combobox.currentText()}")
        
    def _update_save_preset_button_state(self):
        """根据当前选择的预设更新保存按钮的状态"""
        # 当选择"无预设"选项时禁用保存按钮
        preset_index = self.preset_combobox.currentIndex()
        self.save_preset_btn.setEnabled(preset_index != 0)
        
    def save_preset(self):
        """保存当前选择的英雄队伍到预设"""
        # 由于按钮已被禁用，这里可以不检查索引0
        
        if not self.champions_data:
            self.status_label.setText("❌ 英雄数据未加载，无法保存预设")
            return
        
        # 获取当前选择的英雄数据
        selected_team = {}
        for position, combo in self.position_comboboxes.items():
            champ_id = combo.currentData()
            if champ_id:
                champ_data = self.champions_data.get(str(champ_id))
                if champ_data:
                    selected_team[position] = {
                        'champion_id': int(champ_id),
                        'name': champ_data['name'],
                        'alias': champ_data['alias'],
                        'primary_position': position
                    }
        
        if len(selected_team) < 5:
            self.status_label.setText("❌ 请为所有位置选择英雄后再保存预设")
            return
        
        # 保存到当前选择的预设
        preset_index = self.preset_combobox.currentIndex()
        self.presets[preset_index] = selected_team.copy()
        
        self.status_label.setText(f"✅ 已保存到 {self.preset_combobox.currentText()}")

    def set_ui_enabled(self, enabled):
        self.generate_btn.setEnabled(enabled)
        self.hero_group.setEnabled(enabled)
        self.execute_btn.setEnabled(enabled)
        self.room_name_input.setEnabled(enabled)
        self.room_password_input.setEnabled(enabled)
        self.preset_combobox.setEnabled(enabled)

    def closeEvent(self, event):
        # 优化关闭流程，确保所有线程都被正确终止
        if self.worker_thread and self.worker_thread.isRunning():
            self.worker_thread.stop()  # 中止工作线程
            self.worker_thread.wait(500)  # 等待最多0.5秒

        if self.connection_checker and self.connection_checker.isRunning():
            self.connection_checker.stop()  # 停止连接检查线程

        # 保存预设列表到文件
        try:
            with open('presets', 'w', encoding='utf-8') as f:
                # 转换预设数据为可序列化的格式
                serializable_presets = []
                for preset in self.presets:
                    if preset is not None:
                        # 只保存必要的信息
                        serializable_preset = {pos: {'champion_id': data['champion_id'], 'name': data['name']} 
                                              for pos, data in preset.items()}
                        serializable_presets.append(serializable_preset)
                    else:
                        serializable_presets.append(None)
                json.dump(serializable_presets, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"保存预设失败: {e}")

        # 立即接受关闭事件，不等待
        event.accept()


def main():
    app = QApplication(sys.argv)
    window = AIBotManagerUI()
    window.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
