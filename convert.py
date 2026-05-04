import os
import sys
import requests
import subprocess

# Usage helper
if len(sys.argv) != 2:
    print("USAGE: python %s <url>" % os.path.basename(__file__))
    exit(0)

normal_piece_dict = { "FU": "歩",
                      "KY": "香",
                      "KE": "桂",
                      "GI": "銀",
                      "KI": "金",
                      "KA": "角",
                      "HI": "飛",
                      "OU": "玉",
                      "TO": "歩成",
                      "NY": "香成",
                      "NK": "桂成",
                      "NG": "銀成",
                      "UM": "角成",
                      "RY": "飛成" }

promo_piece_dict = { "TO": "と",
                     "NY": "成香",
                     "NK": "成桂",
                     "NG": "成銀",
                     "UM": "馬",
                     "RY": "龍" }

def string_to_piece(string, promoted):
    return promo_piece_dict[string] if promoted else normal_piece_dict[string]

promoted_map = [[False for _ in range(10)] for _ in range(10)]

# Parse the website source and store the useful bits
source = str(requests.get(sys.argv[1]).content)
source = source.split("gameHash")[1]
source = source.split("userConfig")[0].replace("&quot;", '')[7:-4].split("-", 2)[2]

datetime = source.split(",", 1); source = datetime[1]
date = datetime[0][:8]
time = datetime[0][9:]

metadata = source.split(",", 11); moves = metadata[-1][8:]
gtype     = metadata[0].split(":", 1)[1]
opp_type  = metadata[1].split(":", 1)[1]
sente     = metadata[2].split(":", 1)[1]
gote      = metadata[3].split(":", 1)[1]
sente_dan = metadata[4].split(":", 1)[1]
gote_dan  = metadata[5].split(":", 1)[1]
result    = metadata[8].split(":", 1)[1].split('_')

# Create output file
out_path = "C:/Users/Terje/Desktop/"
out_name = "%s-%s-%s.kif" % (sente, gote, datetime[0])

out_file = open(out_path + out_name, 'w', encoding="utf-8")

# Variables for use in header
date_print = "%s/%s/%s" % (date[:4], date[4:6], date[6:8])
time_print = "%s:%s:%s" % (time[:2], time[2:4], time[4:6])

gtype_print =        "3切" if gtype == "sb" else "10秒将棋" if gtype == "s1" else "10切"
mochijikan  = "3分切れ負け" if gtype == "sb" else "10分切れ負け"

# Write file header
out_file.write("開始日時：%s %s\r\n" % (date_print, time_print))
out_file.write("棋戦：将棋ウォーズ(%s)\r\n" % gtype_print)
if gtype != 's1':
    out_file.write("持ち時間：%s\r\n" % mochijikan)
out_file.write("手合割：平手\r\n")
out_file.write("先手：%s %s\r\n" % (sente, sente_dan))
out_file.write("後手：%s %s\r\n" % (gote, gote_dan))
out_file.write("手数----指手---------消費時間--\r\n")

# Write the moves one at a time
for move_info in moves.split("},{"):
    move_info = move_info.split(',')

    remaining_time = move_info[0][2:]
    move_num       = int(move_info[1][2:]) + 1
    move           = move_info[2][3:]

    orig = move[0:2]
    dest = move[2:4]
    piece = move[4:6]

    already_promoted = False if orig == '00' else promoted_map[int(orig[:1])][int(orig[1:])]

    piece = string_to_piece(piece, already_promoted)

    promoted_map[int(orig[:1])][int(orig[1:])] = False
    promoted_map[int(dest[:1])][int(dest[1:])] = already_promoted or '成' in piece

    orig = '打' if orig == '00' else '(%s)' % orig

    out_file.write("%3d %s%s%s\r\n" % (move_num, dest, piece, orig))

# DRAW_SENNICHI
# SENTE_WIN_TIMEOUT
# SENTE_WIN_TORYO
# SENTE_WIN_CHECKMATE
# SENTE_WIN_DISCONNECT handled as time loss for now

#   14 千日手            ( 0:01/00:00:06)
# まで13手で千日手

#   23 切れ負け           ( 0:11/00:00:25)
# まで22手で時間切れにより後手の勝ち

#   82 投了             ( 0:00/00:00:03)
# まで81手で先手の勝ち

# Write result
method = "千日手" if result[-1] == "SENNICHI" else "切れ負け" if result[-1] == "TIMEOUT" else "投了"
out_file.write("%3d %s\r\n" % (move_num+1, method))

winner = "先手"           if result[0] == "SENTE"     else "後手"
timeout = "時間切れにより" if result[-1] == "TIMEOUT"  else ""
result = "千日手"         if result[-1] == "SENNICHI" else "%sの勝ち" % winner
out_file.write("まで%d手で%s%s\r\n" % (move_num, timeout, result))

# Done writing
out_file.close()

# Convert to SJIS encoding so Shogi_GUI can open it
subprocess.run('iconv -f UTF-8 -t SJIS %s > "temp_%s"' % (out_name, out_name), cwd=out_path, shell=True)
subprocess.run('mv -f "temp_%s" %s' % (out_name, out_name), cwd=out_path)

# Open in default program (Shogi_GUI)
subprocess.run('start %s' % out_name, cwd=out_path, shell=True)