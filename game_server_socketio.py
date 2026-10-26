"""
트러스트 노 치이카와 (Chiikawa x Trust No Bunny) - Socket.IO 기반 백엔드 게임 서버 (v8.0)

[Socket.IO 도입의 주요 이점]
1. 이벤트 기반 데코레이터 라우팅 (@sio.on('JOIN'), @sio.on('VOTE') 등)
2. 내장 룸(Room) 관리 (sio.enter_room, sio.emit(..., room=room_id))
3. 모바일 자동 재연결 및 핑/퐁 커넥션 유지
4. 소통 격리(성벽/마을) 및 괴물 속닥이기 채널 타겟팅 우수

[필요 라이브러리 설치 (실제 개발 환경)]
pip install python-socketio uvicorn fastapi
"""

import random
import asyncio
from typing import Dict, List, Any, Optional
import socketio
from fastapi import FastAPI
import uvicorn

# 전투 정산 엔진 불러오기
from battle_engine import BattleEngine


# ===================================================================
# 1. 게임 룸 코어 로직 (GameRoom Class)
# ===================================================================
class GameRoom:
    def __init__(self, room_id: str):
        self.room_id = room_id
        self.phase = "LOBBY"  # LOBBY, SETUP, MAYOR_SELECT, NOMINATING, VOTING, ACTION_PHASE, RESOLUTION, GAME_OVER
        self.current_round = 1
        self.max_rounds = 5
        
        # 2중 성벽 HP (v1.0)
        self.wall_hp = 20
        self.watchtower_hp = 25
        
        # 라운드별 상태
        self.failed_votes_count = 0  # 부결 횟수
        self.mayor_id = None         # 매 라운드 무작위 선출
        self.expedition_ids = []     # 토벌조 플레이어 ID 목록
        
        # 데이터 수집
        self.votes: Dict[str, bool] = {}                       # {player_id: True/False}
        self.expedition_submissions: Dict[str, Dict] = {}      # {player_id: {...}}
        self.village_actions: Dict[str, Dict] = {}             # {player_id: {...}}
        
        # 플레이어 데이터 dict: { player_id -> { player_id, name, character, role, hand, item, zone, is_mayor, is_ready, ... } }
        self.players: Dict[str, Dict[str, Any]] = {}

    def add_player(self, player_id: str, name: str, character: str = "CHIIKAWA") -> bool:
        if len(self.players) >= 8:
            return False
        if player_id not in self.players:
            self.players[player_id] = {
                "player_id": player_id,
                "name": name,
                "character": character,
                "role": "CITIZEN",
                "hand": [],
                "item": None,
                "zone": "VILLAGE",
                "is_mayor": False,
                "is_ready": False,
                "last_action": None,
                "momonga_no_vote_count": 0,
                "shisa_last_action_type": None,
                "shisa_consecutive_count": 0,
                "individual_win": False
            }
        else:
            self.players[player_id]["name"] = name
        return True

    def toggle_ready(self, player_id: str, is_ready: bool):
        if player_id in self.players:
            self.players[player_id]["is_ready"] = is_ready

    def setup_game(self):
        """게임 시작 시 정체(괴물 2명), 아이템 1장, 초기 숫자 카드 3장 배부"""
        self.phase = "SETUP"
        self.current_round = 1
        self.wall_hp = 20
        self.watchtower_hp = 25
        
        pids = list(self.players.keys())
        random.shuffle(pids)
        
        # 괴물(MONSTER) 2명 지정
        monster_pids = pids[:2]
        
        available_characters = [
            "CHIIKAWA", "HACHIWARE", "USAGI", "MOMONGA",
            "KURIMANJU", "SHISA", "RAKKO", "FURUHONYA"
        ]
        random.shuffle(available_characters)

        items = [
            "RAKKO_SWORD", "PAJAMAS", "DEKATSUYO", "RAMEN",
            "LOAN_WEAPON", "WORKER_BELL", "CROWN", "POISON_MUSHROOM"
        ]

        for i, pid in enumerate(pids):
            p = self.players[pid]
            p["character"] = available_characters[i % len(available_characters)]
            p["role"] = "MONSTER" if pid in monster_pids else "CITIZEN"
            p["hand"] = [random.randint(1, 4) for _ in range(3)]
            p["item"] = random.choice(items)
            p["zone"] = "VILLAGE"
            p["is_mayor"] = False
            p["is_ready"] = True
            p["momonga_no_vote_count"] = 0
            p["shisa_consecutive_count"] = 0
            p["shisa_last_action_type"] = None
            p["individual_win"] = False

        self.start_round(1)

    def start_round(self, round_num: int):
        self.current_round = round_num
        self.phase = "MAYOR_SELECT"
        self.failed_votes_count = 0
        self.expedition_ids = []
        self.votes = {}
        self.expedition_submissions = {}
        self.village_actions = {}
        
        pids = list(self.players.keys())
        self.mayor_id = random.choice(pids)
        for pid in pids:
            self.players[pid]["is_mayor"] = (pid == self.mayor_id)
            self.players[pid]["zone"] = "VILLAGE"

        self.phase = "NOMINATING"

    def nominate_expedition(self, mayor_id: str, target_ids: List[str]) -> bool:
        if self.phase != "NOMINATING" or mayor_id != self.mayor_id:
            return False
            
        req_count = 3 if self.current_round == 1 else 4
        if len(target_ids) != req_count:
            return False

        self.expedition_ids = target_ids
        self.phase = "VOTING"
        self.votes = {}
        return True

    def process_vote(self, player_id: str, vote: bool) -> Optional[Dict[str, Any]]:
        if self.phase != "VOTING":
            return None

        self.votes[player_id] = vote

        p = self.players.get(player_id)
        if p and p["character"] == "MOMONGA" and not vote:
            p["momonga_no_vote_count"] += 1
            if p["momonga_no_vote_count"] >= 2:
                p["individual_win"] = True

        if len(self.votes) == len(self.players):
            yes_count = sum(1 for v in self.votes.values() if v)
            no_count = sum(1 for v in self.votes.values() if not v)

            if yes_count > no_count:
                self.phase = "ACTION_PHASE"
                for pid, player in self.players.items():
                    player["zone"] = "EXPEDITION" if pid in self.expedition_ids else "VILLAGE"
                return {"passed": True, "yes": yes_count, "no": no_count}
            else:
                self.failed_votes_count += 1
                self.phase = "NOMINATING"
                self.votes = {}
                return {
                    "passed": False,
                    "yes": yes_count,
                    "no": no_count,
                    "failed_count": self.failed_votes_count
                }

        return None

    def submit_expedition_card(self, player_id: str, cards: List[int], card_types: List[str]) -> bool:
        if self.phase != "ACTION_PHASE" or player_id not in self.expedition_ids:
            return False

        p = self.players[player_id]
        for c in cards:
            if c in p["hand"]:
                p["hand"].remove(c)

        self.expedition_submissions[player_id] = {
            "submitted_cards": cards,
            "card_types": card_types
        }
        return True

    def submit_village_action(self, player_id: str, action: str, target_id: Optional[str] = None, cards: Optional[List[int]] = None) -> bool:
        if self.phase != "ACTION_PHASE" or player_id in self.expedition_ids:
            return False

        p = self.players[player_id]
        valid_actions = ["BLESS", "PREDICT"]
        if p["character"] == "USAGI" and p["role"] == "MONSTER":
            valid_actions.append("ATTACK")

        if action not in valid_actions:
            return False

        submitted_cards = cards or []
        if action == "ATTACK" and p["character"] == "USAGI" and p["role"] == "MONSTER":
            for c in submitted_cards:
                if c in p["hand"]:
                    p["hand"].remove(c)

        self.village_actions[player_id] = {
            "action": action,
            "target_id": target_id,
            "submitted_cards": submitted_cards
        }
        return True

    def is_action_phase_complete(self) -> bool:
        exp_done = len(self.expedition_submissions) == len(self.expedition_ids)
        vil_count = len(self.players) - len(self.expedition_ids)
        vil_done = len(self.village_actions) == vil_count
        return exp_done and vil_done

    def resolve_round_battle(self) -> Dict[str, Any]:
        self.phase = "RESOLUTION"

        expedition_data = []
        for pid in self.expedition_ids:
            p = self.players[pid]
            sub = self.expedition_submissions.get(pid, {"submitted_cards": [], "card_types": []})
            is_repeat = (p["last_action"] == "EXPEDITION")
            p["last_action"] = "EXPEDITION"

            expedition_data.append({
                "player_id": pid,
                "name": p["name"],
                "character": p["character"],
                "role": p["role"],
                "item": p["item"],
                "submitted_cards": sub["submitted_cards"],
                "card_types": sub["card_types"],
                "is_repeat_action": is_repeat
            })

        village_data = []
        for pid, p in self.players.items():
            if pid not in self.expedition_ids:
                v_act = self.village_actions.get(pid, {})
                village_data.append({
                    "player_id": pid,
                    "name": p["name"],
                    "character": p["character"],
                    "role": p["role"],
                    "village_action": v_act.get("action"),
                    "submitted_cards": v_act.get("submitted_cards", [])
                })

        battle_result = BattleEngine.calculate_battle(
            round_num=self.current_round,
            failed_votes_count=self.failed_votes_count,
            wall_hp=self.wall_hp,
            watchtower_hp=self.watchtower_hp,
            expedition_players=expedition_data,
            village_players=village_data
        )

        for pid, p in self.players.items():
            if p["character"] == "SHISA" and p["role"] == "CITIZEN":
                curr_act_type = "EXPEDITION" if pid in self.expedition_ids else self.village_actions.get(pid, {}).get("action")
                if curr_act_type in ["EXPEDITION", "BLESS", "PREDICT"]:
                    if p.get("shisa_last_action_type") == curr_act_type:
                        p["shisa_consecutive_count"] = p.get("shisa_consecutive_count", 0) + 1
                    else:
                        p["shisa_last_action_type"] = curr_act_type
                        p["shisa_consecutive_count"] = 1
                    if p["shisa_consecutive_count"] >= 3:
                        p["individual_win"] = True

        self.wall_hp = battle_result["wall_hp"]
        self.watchtower_hp = battle_result["watchtower_hp"]

        self._process_village_rewards(battle_result["attack_success"])

        if self.watchtower_hp <= 0:
            self.phase = "GAME_OVER"
            battle_result["winner"] = "MONSTER"
        elif self.current_round >= self.max_rounds:
            self.phase = "GAME_OVER"
            battle_result["winner"] = "CITIZEN"

        return battle_result

    def _process_village_rewards(self, attack_success: bool):
        for pid, p in self.players.items():
            if pid not in self.expedition_ids:
                p["hand"].append(random.randint(1, 4))
                v_act = self.village_actions.get(pid, {})
                act_type = v_act.get("action")
                target_id = v_act.get("target_id")

                if act_type == "BLESS" and target_id in self.players:
                    bless_card = random.randint(1, 4)
                    if self.players[target_id]["character"] == "MOMONGA" and self.players[target_id]["role"] == "CITIZEN":
                        bless_card = 4
                    self.players[target_id]["hand"].append(bless_card)

                elif act_type == "PREDICT":
                    predicted_damage = v_act.get("predicted_damage", False)
                    if predicted_damage == attack_success:
                        p["hand"].append(random.randint(1, 4))

    def get_private_state(self, player_id: str) -> Dict[str, Any]:
        me = self.players.get(player_id)
        if not me:
            return {}

        public_players = []
        host_pid = list(self.players.keys())[0] if self.players else None
        for pid, p in self.players.items():
            public_players.append({
                "player_id": pid,
                "name": p["name"],
                "character": p["character"],
                "zone": p["zone"],
                "is_mayor": p["is_mayor"],
                "is_host": (pid == host_pid),
                "is_ready": p.get("is_ready", False),
                "hand_count": len(p["hand"])
            })

        return {
            "type": "STATE_UPDATE",
            "room_id": self.room_id,
            "phase": self.phase,
            "current_round": self.current_round,
            "failed_votes_count": self.failed_votes_count,
            "wall_hp": self.wall_hp,
            "watchtower_hp": self.watchtower_hp,
            "me": {
                "player_id": player_id,
                "name": me["name"],
                "character": me["character"],
                "role": me["role"],
                "hand": list(me["hand"]),
                "item": me["item"],
                "is_mayor": me["is_mayor"],
                "is_ready": me.get("is_ready", False),
                "zone": me["zone"],
                "momonga_no_vote_count": me.get("momonga_no_vote_count", 0),
                "shisa_consecutive_count": me.get("shisa_consecutive_count", 0),
                "individual_win": me.get("individual_win", False)
            },
            "expedition_ids": self.expedition_ids,
            "players": public_players
        }


# ===================================================================
# 2. Socket.IO 서버 구현 (python-socketio + FastAPI)
# ===================================================================
sio = socketio.AsyncServer(async_mode='asgi', cors_allowed_origins='*')
fastapi_app = FastAPI(title="Trust No Chiikawa Socket.IO Game Server")
app = socketio.ASGIApp(sio, fastapi_app)

ROOMS: Dict[str, GameRoom] = {}
# socket_id (sid) -> {"room_id": ..., "player_id": ...}
SID_MAP: Dict[str, Dict[str, str]] = {}
# (room_id, player_id) -> socket_id (sid)
PLAYER_SID_MAP: Dict[tuple, str] = {}


async def broadcast_private_states(room_id: str, room: GameRoom):
    """비대칭 정보 은닉: 각 플레이어별 세션(sid)에 개별 맞춤 패킷 발송"""
    for pid in room.players.keys():
        sid = PLAYER_SID_MAP.get((room_id, pid))
        if sid:
            private_state = room.get_private_state(pid)
            await sio.emit("STATE_UPDATE", private_state, to=sid)


@sio.event
async def connect(sid, environ):
    print(f"🔌 Socket.IO 클라이언트 연결: {sid}")


@sio.event
async def disconnect(sid):
    info = SID_MAP.pop(sid, None)
    if info:
        room_id = info["room_id"]
        player_id = info["player_id"]
        PLAYER_SID_MAP.pop((room_id, player_id), None)
        print(f"❌ 클라이언트 퇴장: {player_id} (방: {room_id})")


# 1. 방 입장 (JOIN)
@sio.on("JOIN")
async def on_join(sid, data):
    room_id = str(data.get("room_id", "1000"))
    player_id = str(data.get("player_id", sid))
    name = data.get("name", "치이카와")

    if room_id not in ROOMS:
        ROOMS[room_id] = GameRoom(room_id)
    room = ROOMS[room_id]

    room.add_player(player_id, name)
    SID_MAP[sid] = {"room_id": room_id, "player_id": player_id}
    PLAYER_SID_MAP[(room_id, player_id)] = sid

    # Socket.IO 내장 룸 참가
    sio.enter_room(sid, room_id)
    await broadcast_private_states(room_id, room)


# 2. 준비 상태 변경 (TOGGLE_READY)
@sio.on("TOGGLE_READY")
async def on_toggle_ready(sid, data):
    info = SID_MAP.get(sid)
    if not info: return
    room = ROOMS.get(info["room_id"])
    if not room: return

    is_ready = data.get("ready", False)
    room.toggle_ready(info["player_id"], is_ready)
    await broadcast_private_states(info["room_id"], room)


# 3. 게임 시작 (START_GAME)
@sio.on("START_GAME")
async def on_start_game(sid, data):
    info = SID_MAP.get(sid)
    if not info: return
    room_id = info["room_id"]
    room = ROOMS.get(room_id)
    if not room or room.phase != "LOBBY": return

    if len(room.players) < 5:
        await sio.emit("ERROR", {
            "message": f"현재 인원은 {len(room.players)}명입니다. 최소 5명 이상 모여야 시작할 수 있습니다."
        }, to=sid)
        return

    room.setup_game()
    await broadcast_private_states(room_id, room)


# 4. 경비팀 지명 (NOMINATE)
@sio.on("NOMINATE")
async def on_nominate(sid, data):
    info = SID_MAP.get(sid)
    if not info: return
    room_id = info["room_id"]
    room = ROOMS.get(room_id)
    if not room: return

    target_ids = data.get("target_ids", [])
    if room.nominate_expedition(info["player_id"], target_ids):
        await broadcast_private_states(room_id, room)


# 5. 찬반 투표 (VOTE)
@sio.on("VOTE")
async def on_vote(sid, data):
    info = SID_MAP.get(sid)
    if not info: return
    room_id = info["room_id"]
    room = ROOMS.get(room_id)
    if not room: return

    vote_val = data.get("vote", True)
    res = room.process_vote(info["player_id"], vote_val)
    if res:
        await sio.emit("VOTE_RESULT", res, room=room_id)
    await broadcast_private_states(room_id, room)


# 6. 행동 제출 (SUBMIT_ACTION)
@sio.on("SUBMIT_ACTION")
async def on_submit_action(sid, data):
    info = SID_MAP.get(sid)
    if not info: return
    room_id = info["room_id"]
    player_id = info["player_id"]
    room = ROOMS.get(room_id)
    if not room: return

    cards = data.get("cards", [])
    card_types = data.get("card_types", ["DEFENSE"] * len(cards))
    action = data.get("action", "BLESS")
    target_id = data.get("target_id")

    if player_id in room.expedition_ids:
        room.submit_expedition_card(player_id, cards, card_types)
    else:
        room.submit_village_action(player_id, action, target_id, cards)

    if room.is_action_phase_complete():
        battle_res = room.resolve_round_battle()
        await sio.emit("BATTLE_RESULT", battle_res, room=room_id)

    await broadcast_private_states(room_id, room)


# 7. 실시간 소통 격리 채팅 (CHAT)
@sio.on("CHAT")
async def on_chat(sid, data):
    info = SID_MAP.get(sid)
    if not info: return
    room_id = info["room_id"]
    sender_id = info["player_id"]
    room = ROOMS.get(room_id)
    if not room: return

    chat_msg = data.get("message", "").trim() if hasattr(data.get("message", ""), "trim") else str(data.get("message", "")).strip()
    channel = data.get("channel", "ALL")

    sender = room.players.get(sender_id)
    if not sender or not chat_msg: return

    is_action_phase = (room.phase == "ACTION_PHASE")
    sender_zone = sender.get("zone", "VILLAGE")

    payload = {
        "sender_id": sender_id,
        "sender_name": sender["name"],
        "character": sender["character"],
        "message": chat_msg,
        "channel": channel,
        "zone": sender_zone,
        "is_isolated": (is_action_phase and channel != "MONSTER")
    }

    # 수신 대상 필터링 방송
    for pid, p in room.players.items():
        target_sid = PLAYER_SID_MAP.get((room_id, pid))
        if not target_sid: continue

        if channel == "MONSTER":
            if p.get("role") == "MONSTER":
                await sio.emit("CHAT_MESSAGE", payload, to=target_sid)
        elif is_action_phase:
            if p.get("zone") == sender_zone:
                await sio.emit("CHAT_MESSAGE", payload, to=target_sid)
        else:
            await sio.emit("CHAT_MESSAGE", payload, to=target_sid)


if __name__ == "__main__":
    uvicorn.run("game_server_socketio:app", host="0.0.0.0", port=8000, reload=True)
