"""Episode scripts for the 《吉鸿昌》 four-part series.

Every episode follows the same retention shape:

* 0-5s   hook       — a conflict or a question, never an establishing date card
* 5-30s  rising     — one concrete action per shot, no narration-only beats
* 30-35s reversal   — the expected outcome is overturned (victory -> disband)
* 35-45s cliffhanger — points straight at the next episode

Nine shots x 5s = 45s, which is the longest episode the current Holopix
balance covers (9 x 289 = 2601 of 2775 points).
"""

from __future__ import annotations

from typing import Any

EPISODES: dict[str, dict[str, Any]] = {
    "ep01": {
        "index": 1,
        "title": "北望",
        "title_card": "北望",
        "title_sub": "吉鸿昌 · 第一集",
        "closing_lines": ["九一八之后，", "北方沦陷大半。"],
        "closing_sub": "一九三一年冬 · 长城",
        "next_ep": "第二集《红石峡》",
        "scenes": [
            {
                "id": "ep1sc1",
                "description": "雪后的长城垛口，一名穿棉大衣的男子扶着城墙向北眺望，远山被长焦压暗",
                "prompt": "长焦固定镜头，雪后明代长城垛口，一名穿棉大衣的男子扶着城砖向北眺望，远处群山被压暗虚化；"
                          "冬日冷白散射光，冷灰调，胶片颗粒。",
                "narration": "一九三一年冬，长城上望不见尽头。",
                "shot_language": {
                    "shot_size": "medium", "camera_movement": "static", "lens_mm": 85,
                    "lighting_key": "overcast_soft", "depth_of_field": "shallow", "color_temperature": "cool",
                },
            },
            {
                "id": "ep1sc2",
                "description": "超远景：雪原上一长列逃难百姓背身入关",
                "prompt": "超远景缓慢横移，雪原上一长列挑着行李的百姓背身向关内走去，队伍蜿蜒消失在风雪里；"
                          "冬日阴天冷光，冷灰调，胶片颗粒。",
                "narration": "九一八之后，北方的城一座接一座失守。",
                "shot_language": {
                    "shot_size": "extreme_wide", "camera_movement": "tracking_left", "lens_mm": 24,
                    "lighting_key": "overcast_soft", "depth_of_field": "deep", "color_temperature": "cool",
                },
            },
            {
                "id": "ep1sc3",
                "description": "关城上褪色的旗与沉默站列的人影",
                "prompt": "中景缓慢推进，城墙关隘上一面褪色、没有文字的旧旗在风里垂着，一排穿棉袄的男子沉默站列；"
                          "阴天冷光，冷灰调，胶片颗粒。",
                "narration": "关隘上，他们奉命不许还击。",
                "shot_language": {
                    "shot_size": "wide", "camera_movement": "dolly_in", "lens_mm": 35,
                    "lighting_key": "overcast_soft", "depth_of_field": "deep", "color_temperature": "cool",
                },
            },
            {
                "id": "ep1sc4",
                "description": "昏暗屋内，手指重重按在摊开的地图上，炭火映脸",
                "prompt": "特写固定镜头，昏暗的屋内一双手掌重重按在一张摊开的地图上，指节用力，炭火的光照着半张脸；"
                          "暖黄火光与深阴影对比，压抑，胶片颗粒。",
                "narration": "吉鸿昌拍案：中国的土地，不能再退。",
                "shot_language": {
                    "shot_size": "close_up", "camera_movement": "static", "lens_mm": 50,
                    "lighting_key": "low_key", "depth_of_field": "shallow", "color_temperature": "mixed",
                },
            },
            {
                "id": "ep1sc5",
                "description": "特写：盖红章的请战电报纸被推回，手把纸攥皱",
                "prompt": "特写固定镜头，桌面上一张盖着红章的电报纸被一只手从画面外推回来，另一只手把纸攥出褶皱；"
                          "煤油灯暖点光，深阴影，胶片颗粒。",
                "narration": "他的请战电报，一封封被退回。",
                "shot_language": {
                    "shot_size": "extreme_close_up", "camera_movement": "static", "lens_mm": 85,
                    "lighting_key": "tungsten_warm", "depth_of_field": "shallow", "color_temperature": "warm",
                },
            },
            {
                "id": "ep1sc6",
                "description": "全景：垛口上孤身男子的背影，面向北方群山，风雪",
                "prompt": "全景固定镜头，垛口上一名穿棉大衣的男子孤身的背影面向北方群山，风雪卷过城砖；"
                          "冷白风雪，冷灰剪影，孤寂，胶片颗粒。",
                "narration": "他站在垛口向北望，风雪不止。",
                "shot_language": {
                    "shot_size": "wide", "camera_movement": "static", "lens_mm": 50,
                    "lighting_key": "silhouette", "depth_of_field": "deep", "color_temperature": "cool",
                },
            },
        ],
    },
    "ep02": {
        "index": 2,
        "title": "红石峡",
        "title_card": "红石峡",
        "title_sub": "吉鸿昌 · 第二集",
        "closing_lines": ["胜仗打完了，", "人却散了。"],
        "closing_sub": "一九三三年 · 察哈尔多伦",
        "next_ep": "第三集《归国》",
        "scenes": [
            {
                "id": "ep2sc1",
                "description": "夜里的战壕，火把下两个士兵压低声音说话，同时回头张望",
                "prompt": "手持近景，火把光映在两张沾满尘土的中国军人脸上，两人压低声音说话，其中一个回头张望黑暗；"
                          "壕沟土壁在背景，夜戏暖火光与深蓝夜空对比，浓重胶片颗粒。",
                "narration": "三天后，我们就要被自己人缴械。",
                "shot_language": {
                    "shot_size": "close_up", "camera_movement": "handheld", "lens_mm": 50,
                    "lighting_key": "low_key", "depth_of_field": "shallow", "color_temperature": "mixed",
                },
            },
            {
                "id": "ep2sc2",
                "description": "察哈尔草原上，抗日同盟军举旗誓师，士兵列队",
                "prompt": "镜头缓慢横移，察哈尔草原上一队穿灰蓝棉军装的士兵举旗列队，"
                          "粗糙素面的褪色白布旗面在风里翻卷，旗面没有文字，旗杆是削光的木杆，远处马匹踏起尘土；"
                          "黎明冷光，胶片颗粒，历史影像质感。",
                "narration": "一九三三年春，抗日同盟军誓师。",
                "shot_language": {
                    "shot_size": "extreme_wide", "camera_movement": "tracking_left", "lens_mm": 24,
                    "lighting_key": "overcast_soft", "depth_of_field": "deep", "color_temperature": "cool",
                },
            },
            {
                "id": "ep2sc3",
                "description": "红石峡峡口，夜色中士兵连夜挖掘战壕、垒砌石垒",
                "prompt": "低机位缓慢推进，明代砖石长城关隘下的战壕与垒石，士兵在夜色中挥锹填土，火把光点摇曳；"
                          "夜戏，暖火光与深蓝夜空对比，浓重胶片颗粒。",
                "narration": "红石峡前，他们连夜挖壕筑垒。",
                "shot_language": {
                    "shot_size": "wide", "camera_movement": "dolly_in", "lens_mm": 35,
                    "lighting_key": "low_key", "depth_of_field": "deep", "color_temperature": "mixed",
                },
            },
            {
                "id": "ep2sc4",
                "description": "拂晓，坡地上的骑兵纵马冲下，硝烟与尘土弥漫",
                "prompt": "手持镜头剧烈晃动并快速推进，黎明前的坡地上大批骑兵伏在马背上冲下，硝烟与尘土弥漫；"
                          "冷蓝调，高对比，动势急促，胶片颗粒。",
                "narration": "拂晓时分，马队冲下坡地。",
                "shot_language": {
                    "shot_size": "medium", "camera_movement": "handheld", "lens_mm": 35,
                    "lighting_key": "low_key", "depth_of_field": "shallow", "color_temperature": "cool",
                },
            },
            {
                "id": "ep2sc5",
                "description": "多伦城内巷战，短兵相接的人影在墙影间交错",
                "prompt": "手持跟拍，多伦城狭窄巷道里两组人影短兵相接，刀光一闪，墙面掠过交错的人影；"
                          "天将亮的冷蓝调，强烈明暗对比，动势急促，胶片颗粒。",
                "narration": "多伦城内，天亮前短兵相接。",
                "shot_language": {
                    "shot_size": "medium", "camera_movement": "handheld", "lens_mm": 35,
                    "lighting_key": "low_key", "depth_of_field": "shallow", "color_temperature": "cool",
                },
            },
            {
                "id": "ep2sc6",
                "description": "多伦城门洞打开，百姓夹道，妇人把水囊递给骑兵",
                "prompt": "镜头从人群后方缓慢升起到街口，夯土城门洞外涌进夹道欢呼的百姓，"
                          "一名中国妇人把水囊递给路过的骑兵，街上挂的是没有文字的褪色布条旗；"
                          "正午阳光，暖黄色调，纪实感，胶片颗粒。",
                "narration": "城门开了，百姓把水囊塞给骑兵。",
                "shot_language": {
                    "shot_size": "wide", "camera_movement": "crane_up", "lens_mm": 35,
                    "lighting_key": "golden_hour", "depth_of_field": "deep", "color_temperature": "warm",
                },
            },
            {
                "id": "ep2sc7",
                "description": "军营电报机房里，一张电报纸被送到桌上，手指按住纸面",
                "prompt": "特写固定镜头，军营木桌上的老式莫尔斯电报机滴滴作响，"
                          "一叠电报纸被推到桌面中央，一只手按住纸面，"
                          "旁边是煤油灯和马灯；夜戏冷蓝顶光，压抑，胶片颗粒。",
                "narration": "一纸电报：南京令停战，就地解散。",
                "shot_language": {
                    "shot_size": "close_up", "camera_movement": "static", "lens_mm": 50,
                    "lighting_key": "low_key", "depth_of_field": "shallow", "color_temperature": "cool",
                },
            },
            {
                "id": "ep2sc8",
                "description": "空荡的营地，盟军旗帜被卷走，步枪留在地上",
                "prompt": "中景缓慢横移，空荡的营地地面上散落着步枪和空水壶，一双手把卷起的旗面收走，只留下旗杆；"
                          "阴天散射光，冷灰，荒凉，胶片颗粒。",
                "narration": "盟军的旗被卷走，枪留在原地。",
                "shot_language": {
                    "shot_size": "medium", "camera_movement": "tracking_left", "lens_mm": 35,
                    "lighting_key": "overcast_soft", "depth_of_field": "deep", "color_temperature": "cool",
                },
            },
            {
                "id": "ep2sc9",
                "description": "火车站月台，列车驶离，他站在原地没有上车",
                "prompt": "中景固定镜头，1930年代北方小站，煤烟滚滚的蒸汽机车拉着木制客车厢缓缓驶离，"
                          "站台是黄土路面，一名穿灰蓝棉军大衣的中国男子站在原地没有上车，"
                          "身后是空荡的站场；黄昏冷灰，孤寂，胶片颗粒。",
                "narration": "火车开了，他没有上车。",
                "shot_language": {
                    "shot_size": "medium", "camera_movement": "static", "lens_mm": 50,
                    "lighting_key": "golden_hour", "depth_of_field": "deep", "color_temperature": "cool",
                },
            },
        ],
    },
    "ep03": {
        "index": 3,
        "title": "归国",
        "title_card": "归国",
        "title_sub": "吉鸿昌 · 第三集",
        "closing_lines": ["他回来了，", "却再没有归处。"],
        "closing_sub": "一九三四年 · 天津 → 北平",
        "next_ep": "第四集《绝笔》",
        "scenes": [
            {
                "id": "ep3sc1",
                "description": "码头边，一只手把旧军牌攥紧又松开",
                "prompt": "特写固定镜头，天津海河木栈桥码头的木栏上，一只男人的手攥紧一块刻字的金属军牌，又缓缓松开；"
                          "背景是虚化的河面、煤烟与雾气，冷灰调，压抑，胶片颗粒。",
                "narration": "他回天津，不是回家。",
                "shot_language": {
                    "shot_size": "close_up", "camera_movement": "static", "lens_mm": 85,
                    "lighting_key": "overcast_soft", "depth_of_field": "shallow", "color_temperature": "cool",
                },
            },
            {
                "id": "ep3sc2",
                "description": "煤油灯旁，一纸公文被展开",
                "prompt": "特写固定镜头，煤油灯的火苗旁，一纸公文被展开，灯光在纸面上跳动，桌上有钢笔与旧怀表；"
                          "暖黄点光，深阴影，胶片颗粒。",
                "narration": "免职令，落在一盏煤油灯下。",
                "shot_language": {
                    "shot_size": "close_up", "camera_movement": "static", "lens_mm": 50,
                    "lighting_key": "tungsten_warm", "depth_of_field": "shallow", "color_temperature": "warm",
                },
            },
            {
                "id": "ep3sc3",
                "description": "火车站售票窗口前，他接过一张通往北平的硬纸车票",
                "prompt": "中景固定镜头，1930年代北方小站木质窗框的售票窗口前，一双戴手套的手把一张硬纸车票推过台面，"
                          "窗口后是模糊的人影；冷白日光，纪实感，胶片颗粒。",
                "narration": "他决定北上，去北平做买卖。",
                "shot_language": {
                    "shot_size": "medium", "camera_movement": "static", "lens_mm": 50,
                    "lighting_key": "overcast_soft", "depth_of_field": "shallow", "color_temperature": "cool",
                },
            },
            {
                "id": "ep3sc4",
                "description": "旅店窗边，同行压低声音劝阻，他摇头",
                "prompt": "过肩近景，旅店窗边，两名男子低声交谈，其中一人摇头，窗外是雨后的街灯；"
                          "夜间冷蓝与昏黄混杂，悬疑，胶片颗粒。",
                "narration": "同行说，这条路是拿命去换。",
                "shot_language": {
                    "shot_size": "medium_close", "camera_movement": "static", "lens_mm": 50,
                    "lighting_key": "low_key", "depth_of_field": "shallow", "color_temperature": "mixed",
                },
            },
            {
                "id": "ep3sc5",
                "description": "雪地，他蹲下把一件东西埋进土里，用力踩实",
                "prompt": "低机位固定镜头，雪地里一名男子蹲着用力把一小件东西埋进土中，起身用靴底踩实，"
                          "呼出的白气；冷白冬日，压抑，胶片颗粒。",
                "narration": "他把军牌埋进雪里。",
                "shot_language": {
                    "shot_size": "medium", "camera_movement": "static", "lens_mm": 50,
                    "lighting_key": "overcast_soft", "depth_of_field": "shallow", "color_temperature": "cool",
                },
            },
            {
                "id": "ep3sc6",
                "description": "夜里旅馆走廊，有人用力拍门，门缝透出微光",
                "prompt": "手持镜头在昏暗的旅馆走廊缓慢推进，一扇木门被人从外面拍响，门缝透出微光；"
                          "夜戏冷蓝与昏黄混杂，悬疑，胶片颗粒。",
                "narration": "夜里有人敲门，他早备好了行装。",
                "shot_language": {
                    "shot_size": "medium", "camera_movement": "handheld", "lens_mm": 35,
                    "lighting_key": "low_key", "depth_of_field": "shallow", "color_temperature": "mixed",
                },
            },
            {
                "id": "ep3sc7",
                "description": "北平胡同口，暗处一双手递出一张照片",
                "prompt": "微距特写，北平胡同口的阴影里，一双手把一张卷角的照片递出画面，"
                          "背景有拉洋车的人影掠过；日间冷灰，光比强烈，悬疑，胶片颗粒。",
                "narration": "他的名字，从一张照片上漏了出去。",
                "shot_language": {
                    "shot_size": "extreme_close_up", "camera_movement": "static", "lens_mm": 85,
                    "lighting_key": "low_key", "depth_of_field": "shallow", "color_temperature": "cool",
                },
            },
            {
                "id": "ep3sc8",
                "description": "巷子里他倒在血泊中，被人从两侧拖走",
                "prompt": "手持俯拍，狭窄胡同的石板路上，一名男子倒在血泊中，两侧有人影俯身将他拖离画面；"
                          "日间冷灰，血色刺目，强烈冲击，胶片颗粒。",
                "narration": "十一月初九，他从血泊里被抬进牢房。",
                "shot_language": {
                    "shot_size": "medium", "camera_movement": "handheld", "lens_mm": 35,
                    "lighting_key": "overcast_soft", "depth_of_field": "deep", "color_temperature": "cool",
                },
            },
            {
                "id": "ep3sc9",
                "description": "粗糙的水泥墙面，刻痕与沾着石灰的手指",
                "prompt": "特写固定镜头，粗糙的水泥墙面上刻痕渐显，一只手的手指沿着刻痕划过，指尖有石灰粉；"
                          "冷灰极简构图，肃穆，胶片颗粒。",
                "narration": "狱中墙上，他刻下两个字。",
                "shot_language": {
                    "shot_size": "close_up", "camera_movement": "static", "lens_mm": 50,
                    "lighting_key": "volumetric", "depth_of_field": "deep", "color_temperature": "cool",
                },
            },
        ],
    },
    "ep04": {
        "index": 4,
        "title": "绝笔",
        "title_card": "绝笔",
        "title_sub": "吉鸿昌 · 第四集",
        "closing_lines": ["恨不抗日死，留作今日羞。", "国破尚如此，我何惜此头。"],
        "closing_sub": "一九三四年十一月二十四日 · 北平陆军监狱",
        "next_ep": "",
        "scenes": [
            {
                "id": "ep4sc1",
                "description": "牢房角落的稻草上，一双手折下一截干枯的树枝",
                "prompt": "特写缓慢推进，牢房角落的稻草上，一双手折下一截干枯的树枝；一束冷光斜照，灰尘浮动，"
                          "冷灰极简，胶片颗粒。",
                "narration": "狱中无笔，他折下一截枯枝。",
                "shot_language": {
                    "shot_size": "close_up", "camera_movement": "dolly_in", "lens_mm": 50,
                    "lighting_key": "volumetric", "depth_of_field": "shallow", "color_temperature": "cool",
                },
            },
            {
                "id": "ep4sc2",
                "description": "微距，墙上四行字迹被一点点刻出，笔尖是折断的树枝",
                "prompt": "微距特写缓慢横移，粗糙墙面上四行字迹被一点点刻出，笔尖是折断的树枝，墙面有暗色水痕；"
                          "冷光侧照，肃穆，胶片颗粒。",
                "narration": "枯枝蘸血，墙上现出四行字。",
                "shot_language": {
                    "shot_size": "extreme_close_up", "camera_movement": "tracking_left", "lens_mm": 85,
                    "lighting_key": "rim_lit", "depth_of_field": "shallow", "color_temperature": "cool",
                },
            },
            {
                "id": "ep4sc3",
                "description": "墙上字迹的特写，镜头停在最后两行",
                "prompt": "特写固定镜头，镜头缓慢上移停在墙上最后两行字迹上，光从侧面切入刻痕；"
                          "冷灰极简，肃穆，胶片颗粒。",
                "narration": "恨不抗日死，留作今日羞。",
                "shot_language": {
                    "shot_size": "close_up", "camera_movement": "tilt_up", "lens_mm": 50,
                    "lighting_key": "rim_lit", "depth_of_field": "shallow", "color_temperature": "cool",
                },
            },
            {
                "id": "ep4sc4",
                "description": "铁牢门缓缓打开，一道缝隙的冷光切入黑暗牢房",
                "prompt": "低机位固定镜头，铁门缓缓打开，一道缝隙的冷光切入黑暗牢房，地面反光；"
                          "极简冷灰，悬疑，胶片颗粒。",
                "narration": "十一月廿四，黎明前，牢门被打开。",
                "shot_language": {
                    "shot_size": "wide", "camera_movement": "static", "lens_mm": 35,
                    "lighting_key": "low_key", "depth_of_field": "deep", "color_temperature": "cool",
                },
            },
            {
                "id": "ep4sc5",
                "description": "黎明前的空地，一名囚服男子抬手整了整衣领",
                "prompt": "中近景固定镜头，黎明前的空地上，一名囚服男子抬手整了整衣领，风吹动他的衣角；"
                          "冷灰逆光剪影，肃穆，胶片颗粒。",
                "narration": "枪响之前，他整了整衣领。",
                "shot_language": {
                    "shot_size": "medium_close", "camera_movement": "static", "lens_mm": 50,
                    "lighting_key": "silhouette", "depth_of_field": "shallow", "color_temperature": "cool",
                },
            },
            {
                "id": "ep4sc6",
                "description": "空地上，他走了三步后停下，回头看向城墙方向",
                "prompt": "全景固定镜头，晨雾中的空地，一名囚服男子走了三步停下，回头望向远处城墙，"
                          "背影被雾吞没；冷灰极简，肃穆，胶片颗粒。",
                "narration": "他走了三步，又回了一次头。",
                "shot_language": {
                    "shot_size": "wide", "camera_movement": "static", "lens_mm": 35,
                    "lighting_key": "volumetric", "depth_of_field": "deep", "color_temperature": "cool",
                },
            },
            {
                "id": "ep4sc7",
                "description": "行刑队举枪的剪影，枪口在雾中",
                "prompt": "低机位固定镜头，雾中一排举枪的士兵剪影，枪口对着画面外，远处城墙模糊；"
                          "冷灰极简，肃穆，胶片颗粒。",
                "narration": "枪响在背后，他没有再回头。",
                "shot_language": {
                    "shot_size": "medium", "camera_movement": "static", "lens_mm": 50,
                    "lighting_key": "low_key", "depth_of_field": "shallow", "color_temperature": "cool",
                },
            },
            {
                "id": "ep4sc8",
                "description": "晨雾中的老城屋顶与远处城墙，地面留着一串脚印",
                "prompt": "镜头从空荡的院落缓慢升高，晨雾中的老城屋顶与远处城墙，地面留着一串脚印；"
                          "冷灰极简，肃穆，胶片颗粒。",
                "narration": "一九三四年十一月二十四日，北平。",
                "shot_language": {
                    "shot_size": "extreme_wide", "camera_movement": "crane_up", "lens_mm": 24,
                    "lighting_key": "overcast_soft", "depth_of_field": "deep", "color_temperature": "cool",
                },
            },
            {
                "id": "ep4sc9",
                "description": "空镜：墙上四行字的定格，光缓慢移过刻痕",
                "prompt": "特写固定镜头，墙上四行字的定格画面，一束光缓慢移过刻痕，灰尘浮动；"
                          "冷灰极简，肃穆，胶片颗粒。",
                "narration": "国破尚如此，我何惜此头。",
                "shot_language": {
                    "shot_size": "close_up", "camera_movement": "static", "lens_mm": 85,
                    "lighting_key": "rim_lit", "depth_of_field": "shallow", "color_temperature": "cool",
                },
            },
        ],
    },
}

# ---------------------------------------------------------------------------
# Era anchoring
# ---------------------------------------------------------------------------
# The Holopix Seedance endpoint takes a prompt and nothing else - there is no
# negative_prompt field - so the only way to keep a 1930s Chinese setting is to
# say, positively, who the people are and what they wear. Without this anchor
# the model defaults to a generic Western war film: white faces in Brodie
# helmets, plastic flags, a modern train. The anchor is prepended to every
# prompt so it lands before the camera language, and the style tail closes the
# prompt with the archival look the series wants.
#
# Keep the anchor short. A long list of props in every prompt makes the model
# leak them into shots where they do not belong (a steam locomotive turning up
# in the trench). Period props belong in the individual shot prompt.

ERA_ANCHOR: dict[str, str] = {
    "ep01": "1931年中国北方长城一带，黑发黄皮肤的中国面孔，"
            "灰蓝色粗布棉袄与土黄色棉衣、皮帽与布鞋",
    "ep02": "1933年中国察哈尔塞北，中国北方军人，黑发黄皮肤的中国面孔，"
            "灰蓝色粗布棉军装与土黄色棉袄、绑腿布鞋、军毡布帽",
    "ep03": "1934年中国天津，中国人面孔，深色长衫与棉袍、毡帽、毡帽平民装束",
    "ep04": "1934年中国北平，中国人面孔，灰色囚服与民国军警呢制服",
}

ERA_TAIL = "；1930年代中国黑白纪实影像，老胶片颗粒与轻微划痕，冷灰调"

for _ep_id, _ep in EPISODES.items():
    _anchor = ERA_ANCHOR.get(_ep_id)
    if not _anchor:
        continue
    for _scene in _ep["scenes"]:
        _scene["prompt"] = f"{_anchor}；{_scene['prompt']}{ERA_TAIL}"
