"""
트러스트 노 치이카와 (Chiikawa x Trust No Bunny) v1.03 - 파이썬 백엔드 전투 정산 엔진

[규칙 반영 내역]
1. 랏코 시민 특성 순서 (v1.03):
   - 랏코의 시민 특성(1, 2점 카드 2배)을 캐릭터 특성 중 가장 후순위로 계산
2. 전투 효과 적용 우선순위 (v1.02):
   - 캐릭터 고유 특성(시민/괴물) 선(先)계산 -> 아이템 효과 후(後)계산
3. 투표 부결 페널티 (v1.01):
   - 1~2라운드: +3점/회 (중첩 적용)
   - 3~5라운드: +5점/회 (중첩 적용)
4. 2중 방어선 시스템:
   - 벽(HP 20) -> 감시탑(HP 25), 초과 피해 소멸
5. 5단계 전투 정산 파이프라인:
   - 1단계: 점수 변환 (기타 캐릭터 특성 -> 랏코 시민 특성(특성 중 후순위) -> 데카츠요 아이템)
   - 2단계: 단순 점수 가산 (랏코의 검 +4점)
   - 3단계: 본인 행동 조건 효과 (시사 시민, 모몽가 괴물, 우사기 괴물 마을 공격)
   - 4단계: 카드 개수 조건 효과 (치이카와 시민/괴물, 대장 갑옷의 왕관)
   - 5단계: 최종 피해 및 HP 감소 판정 (파자마 아이템 max(0, dmg - 4))
"""

from typing import List, Dict, Any, Optional


class BattleEngine:
    # 라운드별 기본 스펙 및 부결 페널티 정의
    BASE_ATTACK = {1: 12, 2: 14, 3: 16, 4: 18, 5: 20}
    FAIL_PENALTY = {1: 3, 2: 3, 3: 5, 4: 5, 5: 5}

    @classmethod
    def calculate_battle(
        cls,
        round_num: int,
        failed_votes_count: int,
        wall_hp: int,
        watchtower_hp: int,
        expedition_players: List[Dict[str, Any]],
        village_players: Optional[List[Dict[str, Any]]] = None,
        base_attack_override: Optional[int] = None
    ) -> Dict[str, Any]:
        """
        v1.03 5단계 전투 정산 파이프라인 실행 함수
        """
        if village_players is None:
            village_players = []

        logs = []
        logs.append(f"=== [ROUND {round_num} 전투 정산 시작 (v1.03)] ===")

        # 0. 기본 공격력 및 중첩 부결 페널티 계산
        base_atk = base_attack_override if base_attack_override is not None else cls.BASE_ATTACK.get(round_num, 12)
        penalty_per_fail = cls.FAIL_PENALTY.get(round_num, 3)
        total_fail_penalty = failed_votes_count * penalty_per_fail
        
        monster_attack = base_atk + total_fail_penalty
        logs.append(f"기본 공격력: {base_atk}점 | 부결 페널티({failed_votes_count}회): +{total_fail_penalty}점 (누적 기본 공격력: {monster_attack}점)")

        # 카드 제출 수 사전 집계 (4단계 및 통계용)
        player_card_counts = {}
        for p in expedition_players:
            pid = p["player_id"]
            cards = p.get("submitted_cards", [])
            player_card_counts[pid] = len(cards)

        # 마을 우사기 괴물 공격 카드 수도 제출 수에 포함
        for vp in village_players:
            if vp.get("character") == "USAGI" and vp.get("role") == "MONSTER" and vp.get("village_action") == "ATTACK":
                v_cards = vp.get("submitted_cards", [])
                player_card_counts[vp["player_id"]] = len(v_cards)

        has_pajamas = False
        processed_expedition = []

        # ==========================================
        # 1단계: 점수 변환 (캐릭터 특성 순서 적용 -> 아이템 효과 순)
        # ==========================================
        logs.append("\n--- [1단계: 점수 변환] ---")
        for p in expedition_players:
            pid = p["player_id"]
            name = p.get("name", pid)
            char = p.get("character")
            role = p.get("role")
            item = p.get("item")
            raw_cards = list(p.get("submitted_cards", []))

            transformed_cards = []
            for val in raw_cards:
                curr_val = val

                # (1-1A) 일반 캐릭터 특성 변환 (선계산)
                # 쿠리만쥬 시민: 3점->1점, 4점->2점 변환 후 1,2점 카드는 2배 (음수 카드 제외)
                if char == "KURIMANJU" and role == "CITIZEN":
                    if curr_val > 0:
                        if curr_val == 3: curr_val = 1
                        elif curr_val == 4: curr_val = 2
                        curr_val *= 2

                # (1-1B) 랏코 시민 특성 변환 (v1.03: 특성 중 가장 후순위로 계산)
                # 랏코 시민: 1점, 2점 카드를 2배로 계산
                if char == "RAKKO" and role == "CITIZEN":
                    if curr_val in (1, 2):
                        curr_val *= 2

                # (1-2) 아이템 효과 변환 (후계산)
                # 데카츠요: 점수 반전 (1<->4, 2<->3, 음수도 반전)
                if item == "DEKATSUYO":
                    flip_map = {1: 4, 2: 3, 3: 2, 4: 1, -1: -4, -2: -3, -3: -2, -4: -1}
                    curr_val = flip_map.get(curr_val, curr_val)

                transformed_cards.append(curr_val)

            logs.append(f"• {name}({char}): 원본 {raw_cards} -> 변환 {transformed_cards}")
            
            p_copy = p.copy()
            p_copy["transformed_cards"] = transformed_cards
            processed_expedition.append(p_copy)

            if item == "PAJAMAS":
                has_pajamas = True

        # ==========================================
        # 2단계: 단순 점수 가산
        # ==========================================
        logs.append("\n--- [2단계: 단순 점수 가산] ---")
        player_scores = {}

        for p in processed_expedition:
            pid = p["player_id"]
            name = p.get("name", pid)
            role = p.get("role")
            item = p.get("item")
            cards = p["transformed_cards"]
            types = p.get("card_types", ["DEFENSE"] * len(cards))

            p_def = 0
            p_atk = 0
            for idx, c_val in enumerate(cards):
                c_type = types[idx] if idx < len(types) else "DEFENSE"
                if role == "MONSTER" and c_type == "ATTACK":
                    p_atk += c_val
                else:
                    p_def += c_val

            # 아이템 [랏코의 검]: 세트 전체에 +4점 가산
            if item == "RAKKO_SWORD":
                p_def += 4
                logs.append(f"• {name}: [랏코의 검] 세트 방어력 +4점 가산")

            player_scores[pid] = {"defense": p_def, "attack": p_atk}
            monster_attack += p_atk

        # ==========================================
        # 3단계: 본인의 행동 조건 효과
        # ==========================================
        logs.append("\n--- [3단계: 본인 행동 조건 효과] ---")
        for p in processed_expedition:
            pid = p["player_id"]
            name = p.get("name", pid)
            char = p.get("character")
            role = p.get("role")

            # 시사 시민: 경비 연속 행동 시 방어 점수 +2점
            if char == "SHISA" and role == "CITIZEN" and p.get("is_repeat_action", False):
                player_scores[pid]["defense"] += 2
                logs.append(f"• {name}: [시사 시민 특성] 경비 연속 행동 방어력 +2점 가산")

            # 모몽가 괴물: 매 라운드 공격력 +3점 가산
            if char == "MOMONGA" and role == "MONSTER":
                monster_attack += 3
                logs.append(f"• {name}: [모몽가 괴물 특성] 공격력 +3점 가산")

        # 우사기 괴물: 마을에서 공격 수행 시 공격력 반영
        for vp in village_players:
            if vp.get("character") == "USAGI" and vp.get("role") == "MONSTER" and vp.get("village_action") == "ATTACK":
                u_cards = vp.get("submitted_cards", [])
                u_atk_sum = sum(u_cards)
                monster_attack += u_atk_sum
                logs.append(f"• 마을 {vp.get('name', vp['player_id'])}: [우사기 괴물 마을 공격] 공격력 +{u_atk_sum}점 반영")

        # ==========================================
        # 4단계: 카드 개수 조건 효과
        # ==========================================
        logs.append("\n--- [4단계: 카드 개수 조건 효과] ---")
        max_card_count = max(player_card_counts.values()) if player_card_counts else 0
        max_card_players = [pid for pid, count in player_card_counts.items() if count == max_card_count]

        for p in processed_expedition:
            pid = p["player_id"]
            name = p.get("name", pid)
            char = p.get("character")
            role = p.get("role")

            # 치이카와 시민: 단독 최다 카드 제출 시 방어 점수 -3점 패널티
            if char == "CHIIKAWA" and role == "CITIZEN":
                if len(max_card_players) == 1 and max_card_players[0] == pid:
                    player_scores[pid]["defense"] -= 3
                    logs.append(f"• {name}: [치이카와 시민 특성] 단독 최다 제출 패널티 -3점 차감")

            # 치이카와 괴물: 3장 이상 제출한 플레이어 1명당 전체 점수 -2점 차감
            if char == "CHIIKAWA" and role == "MONSTER":
                over_three_count = sum(1 for count in player_card_counts.values() if count >= 3)
                deduction = over_three_count * 2
                monster_attack -= deduction
                logs.append(f"• {name}: [치이카와 괴물 특성] 3장 이상 제출자({over_three_count}명) x 2점 = -{deduction}점 차감")

        total_defense = sum(s["defense"] for s in player_scores.values())
        logs.append(f"• 총 방어 수치: {total_defense}점 | 총 괴물 공격 수치: {monster_attack}점")

        # ==========================================
        # 5단계: 최종 피해 및 HP 감소 판정
        # ==========================================
        logs.append("\n--- [5단계: 최종 피해 및 HP 감소 판정] ---")
        raw_damage = max(0, monster_attack - total_defense)

        # 파자마 아이템 효과: 최종 피해 -4점 (최소 0)
        if has_pajamas:
            final_damage = max(0, raw_damage - 4)
            logs.append(f"• [파자마 아이템] 적용: Raw 피해({raw_damage}점) - 4점 = 최종 피해({final_damage}점)")
        else:
            final_damage = raw_damage
            logs.append(f"• 최종 피해: {final_damage}점")

        # 성벽 HP 차감 계산 (벽 HP 20 -> 감시탑 HP 25, 초과 피해 소멸)
        new_wall_hp = wall_hp
        new_watchtower_hp = watchtower_hp
        attack_success = final_damage > 0

        if final_damage > 0:
            if wall_hp > 0:
                new_wall_hp = max(0, wall_hp - final_damage)
                logs.append(f"💥 벽 HP 차감: {wall_hp} -> {new_wall_hp} (초과 피해 소멸)")
            else:
                new_watchtower_hp = max(0, watchtower_hp - final_damage)
                logs.append(f"💥 감시탑 HP 차감: {watchtower_hp} -> {new_watchtower_hp}")
        else:
            logs.append("🛡️ 방어 성공! 피해가 발생하지 않았습니다.")

        # 라운드 후속 카드 수급 플래그 집계
        reward_flags = {
            "hachiware_hp_damaged_cards": [p["player_id"] for p in expedition_players if p.get("character") == "HACHIWARE" and p.get("role") == "CITIZEN" and attack_success],
            "crown_item_winners": [p["player_id"] for p in expedition_players if p.get("item") == "CROWN" and len(max_card_players) == 1 and max_card_players[0] == p["player_id"]]
        }

        return {
            "final_damage": final_damage,
            "attack_success": attack_success,
            "total_attack": monster_attack,
            "total_defense": total_defense,
            "wall_hp": new_wall_hp,
            "watchtower_hp": new_watchtower_hp,
            "game_over": new_watchtower_hp <= 0,
            "reward_flags": reward_flags,
            "logs": logs
        }
