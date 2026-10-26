"""
트러스트 노 치이카와 (Chiikawa x Trust No Bunny) v1.05 - FastAPI WebSocket 백엔드 서버 (v9)

[주요 기능 및 패치 내역]
1. CORS 미들웨어 적용: Netlify 및 외부 도메인과의 통신 차단 방지
2. Render 슬립 깨우기 및 헬스체크 엔드포인트: GET / 및 GET /health 추가
3. 대기실 TOGGLE_READY 및 방장(is_host) 상태 동기화
4. 2중 성벽 시스템 및 전투 엔진(battle_engine.py) 연동
"""

import random
import asyncio
from typing import Dict, List, Any, Optional
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

# v1.03 전투 정산 엔진 불러오기
try:
    from battle_engine import BattleEngine
except ImportError:
    BattleEngine = None


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
        self.failed_votes_count = 0  # 해당 라운드 부결 횟수
        self.mayor_id = None         # 매 라운드 무작위 선출
        self.expedition_ids = []     # 토벌조 플레이어 ID 목록
        
        # 투표 및 제출 수집 데이터
        self.votes: Dict[str, bool] = {}
        self.expedition_submissions: Dict[str, Dict] = {}
        self.village_actions: Dict[str, Dict] = {}
        
        # 플레이어 데이터 dict
        self.players: Dict[str, Dict[str, Any]] = {}

    def add_player(self, player_id: str, name: str, character: str = "CHIIKAWA") -> bool:
        if len(self.players) >= 8:
            return False
        
        is_first = (len(self.players) == 0)
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
                "is_host": is_first,
                "is_ready": False,
                "last_action": None
            }
        else:
            self.players[player_id]["name"] = name
            if character:
                self.players[player_id]["character"] = character
        return True

    def toggle_ready(self, player_id: str, ready: Optional[bool] = None) -> bool:
        if player_id in self.players:
            if ready is not None:
                self.players[player_id]["is_ready"] = ready
            else:
                self.players[player_id]["is_ready"] = not self.players[player_id]["is_ready"]
            return True
        return False

    def setup_game(self):
        """게임 시작 시 정체(괴물 2명), 아이템 1장, 초기 숫자 카드 3장 배부"""
        self.phase = "SETUP"
        self.current_round = 1
        self.wall_hp = 20
        self.watchtower_hp = 25
        
        pids = list(self.players.keys())
        random.shuffle(pids)
        
        monster_pids = pids[:2]
        items = [
            "RAKKO_SWORD", "PAJAMAS", "DEKATSUYO", "RAMEN",
            "LOAN_WEAPON", "WORKER_BELL", "CROWN", "POISON_MUSHROOM"
        ]

        for pid in pids:
            p = self.players[pid]
            p["role"] = "MONSTER" if pid in monster_pids else "CITIZEN"
            p["hand"] = [random.randint(1, 4) for _ in range(3)]
            p["item"] = random.choice(items)
            p["zone"] = "VILLAGE"
            p["is_mayor"] = False

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

        if len(self.votes) == len(self.players):
            yes_count = sum(1 for v in self.votes.values() if v)
            no_count = sum(1 for v in self.votes.values() if not v)

            if yes_count > no_count:
                self.phase = "ACTION_PHASE"
                for pid, p in self.players.items():
                    p["zone"] = "EXPEDITION" if pid in self.expedition_ids else "VILLAGE"
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

        if BattleEngine:
            battle_result = BattleEngine.calculate_battle(
                round_num=self.current_round,
                failed_votes_count=self.failed_votes_count,
                wall_hp=self.wall_hp,
                watchtower_hp=self.watchtower_hp,
                expedition_players=expedition_data,
                village_players=village_data
            )
        else:
            battle_result = {
                "wall_hp": max(0, self.wall_hp - 5),
                "watchtower_hp": self.watchtower_hp,
                "attack_success": True
            }

        self.wall_hp = battle_result["wall_hp"]
        self.watchtower_hp = battle_result["watchtower_hp"]

        self._process_village_rewards(battle_result.get("attack_success", False))

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
        for pid, p in self.players.items():
            public_players.append({
                "player_id": pid,
                "name": p["name"],
                "character": p["character"],
                "zone": p["zone"],
                "is_mayor": p["is_mayor"],
                "is_host": p.get("is_host", False),
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
                "is_host": me.get("is_host", False),
                "is_ready": me.get("is_ready", False),
                "zone": me["zone"]
            },
            "expedition_ids": self.expedition_ids,
            "players": public_players
        }


# ===================================================================
# FastAPI WebSocket 커넥션 매니저 및 서버 구현
# ===================================================================
class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[str, Dict[str, WebSocket]] = {}

    async def connect(self, websocket: WebSocket, room_id: str, player_id: str):
        await websocket.accept()
        if room_id not in self.active_connections:
            self.active_connections[room_id] = {}
        self.active_connections[room_id][player_id] = websocket

    def disconnect(self, room_id: str, player_id: str):
        if room_id in self.active_connections:
            self.active_connections[room_id].pop(player_id, None)

    async def send_private_state(self, room_id: str, player_id: str, room: GameRoom):
        if room_id in self.active_connections and player_id in self.active_connections[room_id]:
            ws = self.active_connections[room_id][player_id]
            state_msg = room.get_private_state(player_id)
            await ws.send_json(state_msg)

    async def broadcast_private_states(self, room_id: str, room: GameRoom):
        for player_id in list(room.players.keys()):
            await self.send_private_state(room_id, player_id, room)

    async def broadcast_event(self, room_id: str, event_data: Dict[str, Any]):
        if room_id in self.active_connections:
            for ws in self.active_connections[room_id].values():
                await ws.send_json(event_data)


app = FastAPI(title="Trust No Chiikawa Game Server")

# CORS 미들웨어 적용
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Render 슬립 해제 및 헬스 체크용 엔드포인트
@app.get("/")
@app.get("/health")
def health_check():
    return {"status": "ok", "message": "Trust No Chiikawa Server Running"}


manager = ConnectionManager()
ROOMS: Dict[str, GameRoom] = {}


@app.websocket("/ws/{room_id}/{player_id}")
async def websocket_endpoint(websocket: WebSocket, room_id: str, player_id: str):
    await manager.connect(websocket, room_id, player_id)
    
    if room_id not in ROOMS:
        ROOMS[room_id] = GameRoom(room_id)
    room = ROOMS[room_id]

    try:
        while True:
            data = await websocket.receive_json()
            event_type = data.get("type")

            # 1. 방 입장 (JOIN)
            if event_type == "JOIN":
                name = data.get("name", player_id)
                character = data.get("character", "CHIIKAWA")
                room.add_player(player_id, name, character)
                await manager.broadcast_private_states(room_id, room)

            # 2. 준비 상태 변경 (TOGGLE_READY)
            elif event_type == "TOGGLE_READY":
                ready_val = data.get("ready")
                room.toggle_ready(player_id, ready_val)
                await manager.broadcast_private_states(room_id, room)

            # 3. 게임 시작 (START_GAME)
            elif event_type == "START_GAME":
                if room.phase == "LOBBY":
                    room.setup_game()
                    await manager.broadcast_private_states(room_id, room)

            # 4. 토벌조 지명 (NOMINATE)
            elif event_type == "NOMINATE":
                target_ids = data.get("target_ids", [])
                if room.nominate_expedition(player_id, target_ids):
                    await manager.broadcast_private_states(room_id, room)

            # 5. 찬반 투표 (VOTE)
            elif event_type == "VOTE":
                vote_val = data.get("vote", True)
                res = room.process_vote(player_id, vote_val)
                if res:
                    await manager.broadcast_event(room_id, {"type": "VOTE_RESULT", "data": res})
                await manager.broadcast_private_states(room_id, room)

            # 6. 행동 제출 (SUBMIT_ACTION)
            elif event_type == "SUBMIT_ACTION":
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
                    await manager.broadcast_event(room_id, {"type": "BATTLE_RESULT", "data": battle_res})

                await manager.broadcast_private_states(room_id, room)

    except WebSocketDisconnect:
        manager.disconnect(room_id, player_id)
        print(f"❌ 클라이언트 연결 해제: {player_id}")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
