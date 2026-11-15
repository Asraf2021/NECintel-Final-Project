# Süni intellekt alətlərindən istifadə haqqında bəyannamə

**Layihə:** Urban Crisis & Crowd Evacuation Engine
**Tarix:** 15.11.2026

## 1. İstifadə olunan alət

| Alət | Növ | Kim istifadə etdi |
|---|---|---|
| **Claude Code** (Anthropic) | Terminalda / bulud mühitində işləyən AI proqramlaşdırma agenti | Komandanın hər üç üzvü (vahid sessiya vasitəsilə) |

Başqa AI alətindən (Cursor, GitHub Copilot, ChatGPT və s.) **istifadə olunmayıb**.

## 2. Mərhələlər üzrə istifadə

| Mərhələ | AI nə etdi | Komanda nə etdi (insan nəzarəti) |
|---|---|---|
| **Tələblərin təhlili** | Tapşırıq mətnini təhlil etdi, tələbləri modullara böldü, aydın olmayan məqamlar üzrə 7 sual verdi | Sualları cavablandırdı: xarici API simulyasiya olunsun, GitHub repo, Windows-da demo, interfeys AZ/RU/EN, sənədlər azərbaycanca |
| **Arxitektura seçimi** | Variantları təklif etdi: modul monolit (FastAPI) + ayrıca simulyasiya olunmuş Xarici API, SQLite, SSE, adapter + circuit breaker | Arxitekturanı və texnologiya yığınını təsdiqlədi |
| **İnterfeys dizaynı** | Komandanın göstərdiyi nümunələri (sığınacaq lokatoru, doluluq legendası, SOS mobil ekranı, insident paneli, dispetçer sistemi) təhlil edib vahid dizayn hazırladı | 5 istinad nümunəsi təqdim etdi, son görünüşü yoxladı |
| **Kod yazılması** | Backend (marşrutlaşdırma, Kritik Böhran Alqoritmi, SOS, təhlükəsizlik), frontend (vətəndaş tətbiqi, dispetçer paneli), Docker və işə salma skriptləri | Funksionallığı tapşırığın tələbləri ilə tutuşdurdu, demo axınını yoxladı |
| **Testlər** | Ayrıca AI alt-agenti pytest test dəsti və yük testi skripti yazdı; əsas agent nəticələri yoxladı | Testlərin işə salınmasını və nəticələrini nəzərdən keçirdi |
| **Yoxlama** | Hər modulu real brauzerdə (Playwright, başsız Chromium) işə salıb ekran görüntüləri ilə yoxladı; tapılan qüsurları düzəltdi | Ekran görüntülərini və canlı demonu yoxladı |
| **Sənədləşmə** | Ayrıca AI alt-agenti README və Texniki izah sənədini koddan oxuyaraq yazdı; uyğunsuzluqları əsas agentə bildirdi, düzəlişlər edildi | Sənədləri oxudu və təsdiqlədi |
| **Challenge Drop həlləri** | Hər yeni tələb üçün dəyişiklik planı və kodu hazırladı (bax: README → "Challenge Drop-lara uyğunlaşma") | Elan olunan tələbi AI-yə ötürdü, həlli qəbul etdi |

## 3. Nəzarət prinsipləri

1. **Faktların yoxlanması.** Sənədlərdəki hər iddia (hədlər, endpoint-lər, rəqəmlər) koddan götürülüb; test və yük testi nəticələri real işə salınmadan köçürülüb, uydurulmayıb.
2. **Avtomatik testlər.** AI-nin yazdığı kodu digər AI alt-agentinin yazdığı müstəqil testlər yoxlayır; testlər tapşırıqdakı sərhəd dəyərlərini (4 SOS / 5 dəqiqə / 500 m, 90% və 95% doluluq) dəqiq sınayır.
3. **Real mühitdə yoxlama.** Hər modul brauzerdə işə salınıb, demo ssenarisi başdan sona keçilib.
4. **Təhlükəsizlik.** Gizli açarlar koda yazılmayıb (mühit dəyişənləri); demo parollar açıq şəkildə "demo" kimi qeyd olunub.
5. **Məsuliyyət.** Yekun qərarları (arxitektura, dizayn, təqdim olunan funksionallıq) komanda verib; kod komandanın GitHub repozitoriyasında saxlanılır və komanda tərəfindən müdafiə olunur.

## 4. Commit-lərdə izlənə bilənlik

AI-nin yaratdığı hər commit `Co-Authored-By: Claude …` sətri ilə işarələnib, buna görə git tarixçəsində AI-nin töhfəsi açıq görünür.
