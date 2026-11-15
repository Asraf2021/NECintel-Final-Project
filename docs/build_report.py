"""Texniki izah sənədinin generatoru:  python docs/build_report.py  ->  docs/Texniki_Izah.docx

Mətn aşağıdakı məlumat strukturlarındadır. Challenge Drop-lar elan olunduqda yalnız
CHALLENGE_DROPS siyahısına element əlavə edib skripti yenidən işə salmaq kifayətdir.

Blok növləri (SECTIONS daxilində):
    ("h2", "mətn")                      — alt başlıq
    ("p", "mətn")                       — abzas
    ("bullets", ["...", "..."])         — siyahı
    ("table", [başlıqlar], [[sətir], ...], [sütun enləri sm])   — cədvəl
    ("drops",)                          — CHALLENGE_DROPS buraya yerləşdirilir
"""
from __future__ import annotations

from pathlib import Path

from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor

OUTPUT = Path(__file__).resolve().parent / "Texniki_Izah.docx"

# ---------------------------------------------------------------------------
# Titul bloku
# ---------------------------------------------------------------------------
TITLE = {
    "project": "Urban Crisis & Crowd Evacuation Engine",
    "subtitle": "Şəhər böhranı və izdihamın təxliyəsi sistemi",
    "doc_type": "Texniki izah sənədi",
    "date": "15.11.2026",
    "team": "Komanda: ________",
}

# ---------------------------------------------------------------------------
# Challenge Drop-lar. Nümunə:
# CHALLENGE_DROPS.append({
#     "title": "Drop 1 (12:00) — ...",
#     "paragraphs": ["Tələb: ...", "Həll: ...", "Dəyişdirilən fayllar: ..."],
# })
# Siyahı boşdursa, yalnız giriş abzasları göstərilir.
# ---------------------------------------------------------------------------
CHALLENGE_DROPS: list[dict] = [
    {
        "title": "Drop 1 (12:00) — Prioritet SOS rolu, əlçatan marşrut və i18n",
        "paragraphs": [
            "Tələb: \"Hərəkət Məhdudiyyətli Şəxs / Xüsusi Qayğıya Ehtiyacı Olan Vətəndaş\" rolu; bu rolun SOS-u avtomatik "
            "\"Kritik Prioritet\" (Priority #1) almalı, ona yalnız əlil arabası / tibbi nəqliyyat üçün əlçatan (panduslu, "
            "maneəsiz) marşrut təyin olunmalı; bütün interfeys AZ və EN dillərində dinamik dəyişməlidir.",
            "Rol modeli: app/core/roles.py-da vətəndaş rolları reyestri yaradıldı. Hər rol məlumat kimi təsvir olunur — SOS "
            "prioriteti, marşrut profili (foot / wheelchair), yalnız əlçatan məntəqə şərti və briqada tələbi "
            "(accessible_transport). Servislər bu siyasəti oxuyur, ona görə yeni rol əlavə etmək kodu dəyişmir.",
            "Prioritet: SOS-un prioriteti server tərəfində vətəndaşın saxlanılmış rolundan təyin olunur; müştəri sorğuda "
            "prioriteti dəyişə bilməz. Prioritet SOS dispetçer panelində həmişə yuxarıda, ayrıca nişan, səs siqnalı və KPI "
            "kartı ilə göstərilir; zonanın riski \"Kritik\" olur. Briqada təyinində əlçatan nəqliyyatı olan ən yaxın briqada "
            "(tibbi üstün) seçilir, belə briqada boş deyilsə — istənilən ən yaxın.",
            "Əlçatan marşrut: yol seqmentlərinə accessible və barrier (pilləkən, yeraltı keçid, dik yoxuş, səki yoxdur), "
            "məntəqələrə accessible atributu əlavə olundu. wheelchair profili maneəli seqmentləri Dijkstra qrafından bağlı "
            "yollarla eyni mexanizmlə çıxarır, sürət 3 km/saat qəbul olunur. Profil həm Xarici API-yə, həm lokal ehtiyat "
            "marşrutlaşdırıcıya ötürülür — kəsinti zamanı da yalnız əlçatan marşrut verilir; avtomatik yenidən "
            "marşrutlaşdırma da rolu nəzərə alır.",
            "i18n: interfeys AZ / EN / RU arasında səhifə yenilənmədən dəyişir; məntəqə, briqada və rayon adları da "
            "tərcümə olunur, sənəd başlığı və ekran oxuyucu etiketləri dildən asılıdır. Lüğətlərin tamlığı avtomatik testlə "
            "yoxlanılır.",
            "Yoxlama: 48 yeni avtomatik test (Drop 1-dən sonra cəmi 293, hamısı uğurlu) — hər rayondan əlçatan marşrut, prioritetin "
            "dəyişdirilə bilməməsi, briqada seçimi, API kəsintisi, köhnə bazanın miqrasiyası, i18n tamlığı.",
        ],
    },
    {
        "title": "Drop 2 (13:00) — Xarici Xəritə API-sinin sıradan çıxması (503 / > 5 s)",
        "paragraphs": [
            "Tələb: xarici API HTTP 503 qaytarır və ya 5 saniyədən çox cavab vermir; sistem çökməməli, Circuit Breaker / "
            "Fallback avtomatik işə düşməli, marşrut lokal ehtiyat bazadan hesablanmalı və istifadəçiyə \"Oflayn / Ehtiyat "
            "marşrut rejimində işləyir\" bildirişi göstərilməlidir.",
            "Həll: adapter + circuit breaker arxitekturası əvvəldən vardı. Timeout 2,5 s-ə endirildi və timeout-da təkrar cəhd "
            "edilmir — istifadəçi heç vaxt 5 s gözləmir; 3 ardıcıl xətadan sonra breaker açılır və sorğular xarici API-ni "
            "gözləmədən cavablanır.",
            "Üç səviyyəli ehtiyat: lokal yol şəbəkəsi üzərində Dijkstra (son məlum bağlı yollarla), son keşlənmiş marşrutlar, "
            "statik koordinat matrisi (düz xətt məsafəsi × yol əmsalı). Ehtiyat zənciri istisna atmır; kəsinti zamanı heç bir "
            "endpoint 500 qaytarmır. Bərpada keş təmizlənir və sistem dərhal onlayn rejimə qayıdır.",
            "Demo: dispetçer panelində ON/OFF açarı (HTTP 503 və ya timeout > 5 s rejimi) simulyasiya olunmuş xarici API "
            "serverini həqiqətən sıradan çıxarır; əlavə olaraq xarici API ayrıca prosesdə işə salınıb bağlana bilər (real "
            "kəsinti). 11 yeni avtomatik test, cəmi 381 test uğurlu.",
        ],
    },
]

# ---------------------------------------------------------------------------
# Sənədin məzmunu
# ---------------------------------------------------------------------------
SECTIONS: list[dict] = [
    {
        "title": "1. Problem və məqsəd",
        "blocks": [
            ("p", "Böyük şəhərdə fövqəladə hal (yanğın, uçqun, daşqın, kütləvi tibbi hadisə) zamanı iki problem eyni "
                  "vaxtda yaranır. Vətəndaş hara getməli olduğunu, ən yaxın toplanış məntəqəsinin dolu olub-olmadığını "
                  "və ora aparan yolun açıq olub-olmadığını bilmir. Dispetçer isə onlarla ayrı-ayrı siqnal arasında "
                  "vəziyyətin nə vaxt kritik həddi keçdiyini vaxtında görə bilmir və qərarı gecikdirir."),
            ("p", "Layihənin məqsədi Bakı şəhəri nümunəsində bu iki tərəfi bir sistemdə birləşdirən işlək prototip "
                  "qurmaqdır: vətəndaşa aydın təxliyə təlimatı və bir toxunuşla SOS imkanı, dispetçerə isə canlı "
                  "vəziyyət mənzərəsi və kritik hallarda hazır, bir kliklə təsdiqlənən qərar təklifi vermək."),
            ("p", "Prototip real işləyən backend, verilənlər bazası və iki istifadəçi interfeysindən ibarətdir. Yol "
                  "şəbəkəsi, toplanış məntəqələri və briqadalar haqqında məlumat, eləcə də xarici xəritə xidməti "
                  "simulyasiya olunub (bax: bölmə 10)."),
        ],
    },
    {
        "title": "2. Həll yanaşması",
        "blocks": [
            ("p", "Sistem iki moduldan ibarətdir və hər ikisi eyni tətbiq tərəfindən təqdim olunur."),
            ("h2", "Modul 1 — Vətəndaş Təhlükəsizlik Tətbiqi"),
            ("bullets", [
                "Mövqe üç üsulla göstərilir: rayon siyahısı, cihazın geolokasiyası və ya xəritədə klik.",
                "Ən yaxın toplanış məntəqələri (sığınacaq, park, stadion) cari doluluq faizi ilə göstərilir.",
                "Tövsiyə olunan məntəqəyə piyada marşrut, məsafə və təxmini müddət verilir.",
                "Məntəqə 90% və ya daha çox dolduqda, yaxud marşrutdakı yol bağlandıqda vətəndaşa avtomatik "
                "alternativ marşrut göndərilir.",
                "Bir toxunuşla SOS: yanğın, uçqun, daşqın, tibbi yardım.",
                "\"Təhlükəsizəm\" düyməsi vətəndaşın aktiv siqnallarını bağlayır və seçilmiş məntəqənin doluluğunu yeniləyir.",
                "İnterfeys üç dildədir: Azərbaycan, rus, ingilis.",
            ]),
            ("h2", "Modul 2 — Fövqəladə Hallar Dispetçer Paneli"),
            ("bullets", [
                "Canlı panel: əsas göstəricilər, xəritə (SOS siqnalları, məntəqələr, briqadalar, bağlı yollar), "
                "rayonlar üzrə risk cədvəli.",
                "Kritik Böhran Alqoritmi kritik vəziyyəti avtomatik aşkarlayır və \"Qırmızı Həyəcan\" kartı yaradır.",
                "Dispetçer qərarı bir kliklə təsdiqləyir: əlavə briqada göndərilir və yeni toplanış məntəqəsi açılır.",
                "Ayrı-ayrı SOS siqnallarına briqada təyini, briqada statuslarının, məntəqələrin və yolların idarəsi.",
                "Bütün əməliyyatlar audit jurnalına yazılır.",
            ]),
            ("h2", "Əsas istifadəçi axınları"),
            ("p", "Təxliyə axını: vətəndaş mövqeyini göstərir → sistem yaxın məntəqələri və marşrutları hesablayır → "
                  "tövsiyə göstərilir və plan saxlanılır → şərait dəyişdikdə (doluluq, bağlı yol) plan yenidən "
                  "hesablanır və yeni marşrut yalnız həmin vətəndaşa çatdırılır."),
            ("p", "Böhran axını: vətəndaşlar SOS göndərir → hər yeni siqnalda klaster qaydası yoxlanılır → hədd "
                  "keçildikdə dispetçer panelində \"Qırmızı Həyəcan\" yaranır → dispetçer bir kliklə təsdiqləyir → "
                  "briqada göndərilir, ehtiyat məntəqə açılır və vətəndaşların xəritəsində görünür."),
        ],
    },
    {
        "title": "3. Arxitektura",
        "blocks": [
            ("p", "Sistem modul monolit kimi qurulub: bir proses, lakin dəqiq ayrılmış qatlar. Asılılıqlar yalnız "
                  "yuxarıdan aşağıya yönəlib: routerlər → servislər → nüvə. Xarici Xəritə və Marşrutlaşdırma API-si "
                  "isə ayrıca servis kimi simulyasiya olunur və əsas tətbiq onunla yalnız HTTP müştərisi vasitəsilə "
                  "əlaqə saxlayır."),
            ("table",
             ["Komponent", "Yeri", "Vəzifəsi"],
             [
                 ["Frontend", "static/", "Üç səhifə: rol seçimi, vətəndaş tətbiqi, dispetçer paneli. Yığma mərhələsi yoxdur."],
                 ["Routerlər", "app/routers/", "HTTP qatı: validasiya, icazə yoxlaması, cavab kodları. Biznes məntiqi daşımır."],
                 ["Servislər", "app/services/", "Biznes qaydaları: təxliyə planı, SOS, böhran alqoritmi, məntəqələr, "
                                                "analitika, marşrutlaşdırma, demo ssenariləri."],
                 ["Nüvə", "app/core/", "SQLite qatı, hadisə şini (SSE), təhlükəsizlik (JWT, PBKDF2, RBAC, rate limit), "
                                       "yol qrafı və Dijkstra."],
                 ["Verilənlər bazası", "SQLite faylı", "Məntəqələr, SOS siqnalları, vətəndaş statusları, təxliyə planları, "
                                                       "briqadalar, həyəcanlar, bağlı yollar, audit, istifadəçilər."],
                 ["Xarici API (simulyasiya)", "external_api/", "Yol şəbəkəsi, bağlı yollar, sıxlıq, marşrut hesablanması; "
                                                               "kəsinti və yol bağlanmasının simulyasiyası."],
             ],
             [3.6, 3.0, 9.9]),
            ("h2", "Məlumat axını"),
            ("p", "Brauzerlər tətbiqlə REST sorğuları və Server-Sent Events (SSE) axını ilə əlaqə saxlayır. Hadisə "
                  "şinində üç ünvanlama səviyyəsi var: ümumi kanal (doluluq, yol dəyişiklikləri — hamıya), əməliyyat "
                  "kanalı (SOS, həyəcan, briqada — yalnız dispetçer panelinə) və fərdi hadisələr (yeni marşrut — "
                  "yalnız aid olduğu vətəndaşa). SSE əlçatmaz olduqda interfeys dövri sorğuya (polling) keçir."),
            ("p", "Fon tapşırığı hər 4 saniyədən bir xarici API-dən yol vəziyyətini (bağlı yollar, sıxlıq) "
                  "sinxronlaşdırır. Dəyişiklik aşkarlandıqda bağlı yollar siyahısı müştərilərə yayılır və təsirlənən "
                  "təxliyə planları yenidən hesablanır."),
            ("h2", "Niyə modul monolit və ayrıca xarici API"),
            ("bullets", [
                "Müsabiqə şəraitində tək proses quraşdırmanı və nümayişi sadələşdirir: bir əmr, əlavə infrastruktur yoxdur.",
                "Qatların ayrılığı dəyişikliyi lokal saxlayır: yeni qayda bir servis faylına, yeni rol bir lüğətə, "
                "yeni endpoint bir routerə toxunur.",
                "Xarici API-nin ayrıca servis olması real inteqrasiyanı modelləşdirir: timeout, təkrar cəhd, kəsinti "
                "və bərpa real HTTP müştərisi üzərində yoxlanılır. Docker-də o, ayrıca konteynerdir; Docker-siz "
                "rejimdə eyni kod proses daxilində (şəbəkəsiz) çağırılır, müştəri tərəfindəki kod yolu dəyişmir.",
                "Real provayderə keçid yalnız RoutingService sinfinin dəyişdirilməsini tələb edir.",
            ]),
        ],
    },
    {
        "title": "4. Seçilmiş texnologiyalar və əsaslandırma",
        "blocks": [
            ("table",
             ["Texnologiya", "Niyə"],
             [
                 ["Python 3.10+ / FastAPI", "Sürətli inkişaf, asinxron giriş-çıxış, Pydantic ilə avtomatik giriş "
                                            "validasiyası, avtomatik OpenAPI sənədləri (/docs)."],
                 ["SQLite (WAL rejimi)", "Quraşdırma tələb etmir, tranzaksiyalıdır, tək fayldır. WAL rejimi oxuma və "
                                         "yazmanın bir-birini bloklamamasını təmin edir. Prototip üçün kifayətdir; "
                                         "sorğular standart SQL-dədir."],
                 ["SSE (WebSocket əvəzinə)", "Canlı yeniləmə birtərəflidir (server → müştəri), əmrlər isə adi REST ilə "
                                             "gedir. SSE adi HTTP üzərində işləyir, brauzer avtomatik yenidən qoşulur, "
                                             "əlavə kitabxana və protokol tələb etmir."],
                 ["Leaflet (lokal saxlanılır)", "Yüngül, açıq mənbəli xəritə kitabxanası. Layihənin daxilində "
                                                "saxlanıldığı üçün CDN-dən asılılıq yoxdur."],
                 ["Sadə JavaScript (yığma mərhələsiz)", "Node.js və ya yığma aləti tələb olunmur; münsif layihəni "
                                                        "yalnız Python ilə işə salır. Mətnlər dil lüğətlərindədir."],
                 ["httpx", "Asinxron HTTP müştərisi; timeout dəstəyi və ASGI nəqliyyatı sayəsində eyni müştəri həm "
                           "şəbəkə üzərindən, həm də proses daxilində işləyir."],
                 ["Docker / docker compose", "Təkrarlana bilən mühit; əsas tətbiq və xarici API ayrı konteynerlərdir, "
                                             "tətbiq imtiyazsız istifadəçi ilə işləyir."],
             ],
             [4.6, 11.9]),
            ("p", "Autentifikasiya (JWT HS256) və parol heşi (PBKDF2) Python-un standart kitabxanası ilə "
                  "reallaşdırılıb; icra üçün cəmi üç xarici asılılıq var: fastapi, uvicorn, httpx."),
        ],
    },
    {
        "title": "5. Əsas alqoritmlər",
        "blocks": [
            ("h2", "5.1. Təxliyə tövsiyəsi və avtomatik yenidən marşrutlaşdırma"),
            ("bullets", [
                "Açıq məntəqələr vətəndaşın mövqeyinə görə düz xətt məsafəsi ilə sıralanır; ən yaxın 8-i namizəd olur. "
                "Onların hamısı doludursa, daha uzaqdakı qəbul edən məntəqələr (4-ə qədər) əlavə edilir.",
                "Bütün namizədlərə marşrutlar xarici API-yə tək toplu sorğu ilə hesablanır.",
                "Məntəqə \"qəbul edən\" sayılır, əgər açıqdırsa və doluluğu 90%-dən azdırsa.",
                "Tövsiyə: marşrutu mövcud olan qəbul edən məntəqələr arasında piyada müddəti ən qısa olan. Heç biri "
                "qəbul etmirsə — marşrutu olanlar arasında doluluğu ən az olan.",
                "Ən yaxın məntəqə tövsiyə edilməyibsə, səbəbi cavabda göstərilir (doludur və ya yol yoxdur).",
                "Vətəndaşın planı (məntəqə və marşrutun yol seqmentləri) bazada saxlanılır.",
                "Məntəqə 90%-ə çatdıqda və ya yol bağlandıqda saxlanmış planlar yoxlanılır: planın məntəqəsi artıq "
                "qəbul etmirsə və ya marşrut bağlı seqmentdən keçirsə, plan yenidən qurulur və nəticə SSE ilə yalnız "
                "həmin vətəndaşa göndərilir.",
            ]),
            ("h2", "5.2. Kritik Böhran Alqoritmi (\"Qırmızı Həyəcan\")"),
            ("table",
             ["Qayda", "Şərt", "Nə vaxt yoxlanılır"],
             [
                 ["A — SOS klasteri", "Son 5 dəqiqədə eyni zonadan (rayon) və ya yeni siqnaldan 500 m radiusda ən azı "
                                      "4 fərqli vətəndaşdan SOS daxil olub.", "Hər yeni SOS siqnalında."],
                 ["B — doluluq", "Açıq toplanış məntəqəsinin doluluğu 95%-dən çoxdur.",
                  "Doluluq hər dəfə dəyişdikdə."],
             ],
             [3.4, 9.0, 4.1]),
            ("bullets", [
                "Fərqli vətəndaşlar sayılır: bir şəxsin təkrar siqnalları klaster yaratmır. Bundan əlavə, eyni "
                "vətəndaşın eyni növ üzrə 30 saniyə ərzində təkrar kliki yeni siqnal yaratmır.",
                "Təkrarlanmanın qarşısı: eyni rayon (qayda A) və ya eyni məntəqə (qayda B) üçün gözləyən həyəcan "
                "varsa, yenisi yaradılmır — mövcud kart yenilənir.",
                "Cooldown: qərar verildikdən sonra 5 dəqiqə ərzində eyni obyekt üçün yeni həyəcan yaradılmır.",
                "Bir kliklə təsdiq tək tranzaksiyada icra olunur: ən yaxın boş briqada göndərilir (üstünlük dominant "
                "təhlükə növünə uyğun briqadaya verilir: yanğın → yanğınsöndürmə, tibbi → təcili tibbi yardım, "
                "uçqun və daşqın → xilasetmə), ən yaxın ehtiyat məntəqə açılır, klasterin siqnalları briqadaya "
                "bağlanır, audit yazılır. Ya hamısı yazılır, ya heç biri.",
                "Təsdiq idempotentdir: artıq qərar verilmiş həyəcanın təkrar təsdiqi heç bir əməliyyatı təkrarlamır.",
                "Bütün hədlər konfiqurasiya parametrləridir və kodu dəyişmədən tənzimlənir.",
            ]),
            ("h2", "5.3. Marşrutlaşdırma: Dijkstra"),
            ("bullets", [
                "Yol şəbəkəsi qeyri-istiqamətli qrafdır; tilin çəkisi keçid müddətidir (dəqiqə): uzunluq × sıxlıq "
                "əmsalı / piyada sürəti (susmaya görə 4,5 km/saat).",
                "Virtual başlanğıc düyünü: vətəndaşın koordinatı ən yaxın 3 qovşağa piyada çıxış xərci ilə bağlanır, "
                "ona görə alqoritm sadəcə ən yaxın qovşağı deyil, ümumi müddəti ən az olan girişi seçir. Təyinat "
                "tərəfdə ən yaxın 2 qovşaq nəzərə alınır.",
                "Bağlı yollar axtarışdan tamamilə çıxarılır; sıxlıq əmsalı tilin çəkisini artırır.",
                "250 m-dən yaxın təyinat üçün birbaşa piyada keçid qaytarılır.",
                "Eyni alqoritm həm simulyasiya olunmuş xarici API-də, həm də lokal ehtiyat marşrutlaşdırıcıda "
                "istifadə olunur.",
            ]),
            ("h2", "5.4. Time-to-Capacity: doluluq proqnozu (əlavə innovasiya)"),
            ("p", "Komandanın beynəlxalq araşdırma sənədinə (xəstəxana tutumu proqnozları və zamanla dəyişən təxliyə "
                  "tələbatı üzrə tədqiqatlar) əsaslanaraq sistem hər məntəqənin 90% kritik həddinə və 100% tam tutuma "
                  "çatmasına qalan vaxtı hesablayır: r = son 10 dəqiqədə xalis artım / dəqiqə; T90 = (0,9·C − N) / r; "
                  "T100 = (C − N) / r. Sənəddəki nümunə (C = 1000, N = 780, r = 4 nəfər/dəq → T90 = 30 dəq, "
                  "T100 = 55 dəq) avtomatik testlə yoxlanılır."),
            ("bullets", [
                "Proqnoz interval və etibarlılıq səviyyəsi (aşağı / orta / yüksək) ilə göstərilir; r ≤ 0 olduqda vaxt "
                "göstərilmir.",
                "Qabaqcadan yönləndirmə: etibarlı proqnoza görə məntəqə vətəndaş çatana qədər 90%-ə çatacaqsa, növbəti "
                "uyğun məntəqə tövsiyə olunur — insan axını məntəqəni doldurmadan əvvəl.",
                "Dispetçer cədvəlində proqnoz sütunu və məlumatın son yenilənmə vaxtı; vətəndaş kartında proqnoz.",
                "Model sabit axın fərziyyəsinə əsaslanır (MVP); gələcəkdə statistik / ML modeli ilə əvəz oluna bilər.",
            ]),
            ("h2", "5.5. Beynəlxalq istinadlar"),
            ("table",
             ["Nümunə", "Layihədə istifadə olunan ideya"],
             [
                 ["SOBO-WEB (Yaponiya)", "Vətəndaş və dispetçer üçün vahid məlumat bazası və canlı hadisə axını."],
                 ["FEMA ESF#6 (ABŞ)", "Məntəqə statusu, tutum, doluluq və əlil arabası üçün əlçatanlıq atributu."],
                 ["Waze for Cities", "Yol bağlanması feed-i və marşrutun avtomatik yenidən hesablanması."],
                 ["RapidSOS (ABŞ)", "Dispetçerə prioritet və briqada ilə kontekstli vahid SOS kartı."],
                 ["Readiness TW (Tayvan)", "GPS → yaxın məntəqə → təhlükəsiz marşrut → xəbərdarlıq axını."],
                 ["Xəstəxana tutumu proqnozları", "Time-to-Capacity proqnozu."],
             ],
             [4.6, 11.9]),
        ],
    },
    {
        "title": "6. Təhlükəsizlik tədbirləri",
        "blocks": [
            ("bullets", [
                "Autentifikasiya: müddətli JWT (HS256); imza sabit vaxtlı müqayisə ilə yoxlanılır.",
                "Parollar: PBKDF2-HMAC-SHA256, 120 000 iterasiya, hər parol üçün təsadüfi duz; mövcud olmayan "
                "istifadəçi üçün də eyni hesablama aparılır.",
                "Avtorizasiya: icazə əsaslı RBAC. Rollar bir lüğətdə təsvir olunub, hər dispetçer endpoint-i konkret "
                "icazə tələb edir (401 / 403).",
                "Giriş validasiyası: bütün gövdə və sorğu parametrləri Pydantic sxemləri ilə yoxlanılır (koordinat "
                "aralıqları, identifikator formatı, icazəli dəyərlər, uzunluq limitləri).",
                "SQL injection-a qarşı: istifadəçi dəyərləri sorğulara yalnız parametr kimi ötürülür.",
                "Sui-istifadəyə qarşı: giriş cəhdlərinə, SOS siqnallarına (vətəndaş və IP üzrə) və ümumi vətəndaş "
                "API-sinə sorğu limiti.",
                "HTTP başlıqları: Content-Security-Policy (skriptlər yalnız öz mənbədən), X-Frame-Options, "
                "X-Content-Type-Options, Referrer-Policy, Permissions-Policy; API cavabları keşlənmir.",
                "Sorğu gövdəsinin ölçüsü 64 KB ilə məhdudlaşdırılıb.",
                "Gözlənilməz xətalarda müştəriyə daxili detal qaytarılmır.",
                "Audit jurnalı: uğurlu və uğursuz girişlər, dispetçerin bütün əməliyyatları.",
                "Kanal ayrılığı: əməliyyat hadisələri yalnız səlahiyyətli panelə, fərdi marşrut yalnız aid olduğu "
                "vətəndaşa çatdırılır.",
            ]),
            ("p", "Demo hesabları (dispatcher, admin) və imza açarı mühit dəyişənləri ilə dəyişdirilir; susmaya görə "
                  "dəyərlər yalnız nümayiş üçündür."),
        ],
    },
    {
        "title": "7. Performans və dayanıqlılıq tədbirləri",
        "blocks": [
            ("h2", "Xarici API-dən asılılığın idarəsi"),
            ("bullets", [
                "Hər çağırış üçün timeout (1,5 s) və bir təkrar cəhd.",
                "Circuit breaker: ardıcıl 3 uğursuzluqdan sonra xarici API 15 saniyə çağırılmır, sonra sınaq sorğusu "
                "ilə yoxlanılır və uğurlu olduqda normal rejim özü bərpa olunur.",
                "Ehtiyat rejimi: xarici API cavab vermədikdə marşrut lokal Dijkstra ilə, yaddaşdakı qraf və son məlum "
                "bağlı yollar əsasında qurulur. Vətəndaş xəta görmür; rejim interfeysdə göstərilir.",
            ]),
            ("h2", "Hesablamanın sorğu yolundan çıxarılması"),
            ("p", "Arxitektura analizindən sonra iki düzəliş edildi. Birincisi, CPU-ağır marşrut hesablaması event "
                  "loop-dan işçi thread-lərə köçürüldü. İkincisi, vətəndaşların yenidən marşrutlaşdırılması HTTP "
                  "sorğusunun içindən fon planlayıcısına çıxarıldı: sorğu dərhal cavab verir, ardıcıl dəyişikliklər bir "
                  "hesablamada birləşdirilir, nəticə SSE ilə çatdırılır. Real şəhər miqyasında (4 900 qovşaq) \"yolu bağla\" "
                  "sorğusunun cavab müddəti 981 ms-dən 12 ms-ə düşdü, digər sorğuların maksimal gecikməsi 106 ms-dən "
                  "75 ms-ə endi. Paralellik dərəcəsi (1) ölçmə ilə seçilib: Python GIL səbəbindən daha çox thread "
                  "gecikməni artırır."),
            ("h2", "Yük altında davranış"),
            ("bullets", [
                "Marşrut keşi (20 s; yol vəziyyəti dəyişdikdə etibarsız olur) və toplu marşrut sorğusu.",
                "Dispetçer icmalının 1 saniyəlik mikro-keşi.",
                "SQLite WAL rejimi, indekslər, qısa tranzaksiyalar.",
                "SSE növbələri məhduddur (hər müştəriyə 200 hadisə): yavaş müştəri serveri bloklamır.",
                "Bir dövrdə yenidən hesablanan planların sayı məhdudlaşdırılıb (500).",
                "Fon sinxronizasiyası istənilən xətadan sonra işinə davam edir.",
                "Təkrarlanan əməliyyatlar (SOS-un təkrar kliki, ikinci təsdiq) vəziyyəti pozmur.",
            ]),
        ],
    },
    {
        "title": "8. Test strategiyası",
        "blocks": [
            ("table",
             ["Səviyyə", "Hədəf"],
             [
                 ["Vahid (unit) testlər", "Alqoritmlər ayrılıqda: Dijkstra (bağlı yol, sıxlıq), doluluq hədləri, klaster "
                                          "qaydası, token və parol funksiyaları."],
                 ["API testləri", "Endpoint-lər tam tətbiq üzərində: validasiya, icazələr (401 / 403), sorğu limiti, "
                                  "SOS → həyəcan → təsdiq axını, idempotentlik."],
                 ["Dayanıqlılıq testləri", "Xarici API-nin kəsilməsi: ehtiyat rejiminə keçid, circuit breaker, bərpa."],
                 ["Yük testi", "scripts/load_test.py — işlək serverə paralel virtual istifadəçilərlə yük; istifadəçi sayı və "
                               "müddət parametrlə verilir."],
             ],
             [4.6, 11.9]),
            ("p", "Testlər pytest ilə yazılıb və tests/ qovluğundadır. Tətbiq fabriki (create_app) testə ayrıca baza "
                  "yolu verməyə və fon tapşırığını söndürməyə imkan verir, xarici API simulyatoru isə proses daxilində "
                  "işləyir — ona görə testlər şəbəkə və əlavə servis tələb etmir. İşə salma: python -m pytest -q. "
                  "Nəticələr README-də göstərilib."),
            ("p", "Nəticə: 390 avtomatik test, hamısı uğurlu (≈25 saniyə). Testlər tapşırığın sərhəd dəyərlərini dəqiq "
                  "yoxlayır: 3 və 4 fərqli SOS, eyni vətəndaşdan təkrar siqnal, 5 dəqiqəlik pəncərə, 500 m radius, "
                  "doluluğun dəqiq 95% və 95%-dən yuxarı olması, 90% marşrut həddi. Test yazılarkən 5 real qüsur "
                  "aşkarlandı və düzəldildi (faiz hədlərinin yuvarlaqlaşdırılmış dəyərlə müqayisəsi, vətəndaşın "
                  "məntəqədə iki dəfə sayılması, rayon sərhədində təkrar həyəcan, 413 cavabında təhlükəsizlik "
                  "başlıqları, chunked gövdə ölçüsü)."),
            ("p", "Yük testi (200 virtual istifadəçi, 15 saniyə, bir server prosesi): 8 372 sorğu, ~543 sorğu/s, "
                  "0 xəta; doyma rejimində p95 = 279 ms. Real tempdə (istifadəçi başına 1 saniyə fasilə) "
                  "p50 = 2,9 ms, p95 = 8,3 ms."),
        ],
    },
    {
        "title": "9. Challenge Drop dəyişikliklərinə uyğunlaşma",
        "blocks": [
            ("p", "Arxitektura müsabiqə zamanı elan olunan gözlənilməz dəyişikliklər üçün əvvəlcədən çeviklik nəzərdə "
                  "tutur. Gözlənilən dörd kateqoriya üzrə hazır mexanizmlər:"),
            ("table",
             ["Kateqoriya", "Hazır mexanizm"],
             [
                 ["Yeni rol", "Rollar app/core/security.py faylındakı ROLES lüğətindədir (rol → icazələr). Endpoint-lər "
                              "rolu deyil, icazəni yoxlayır, ona görə yeni rol routerlərə toxunmadan əlavə olunur."],
                 ["Yeni dil", "Mətnlər static/js/i18n.js lüğətlərindədir; yeni dil — bir yeni lüğət. Backend mətn "
                              "əvəzinə kod qaytarır."],
                 ["Xarici API kəsintisi", "RoutingService (app/services/routing.py): timeout, təkrar cəhd, circuit "
                                          "breaker, lokal Dijkstra ilə ehtiyat rejimi, son məlum bağlı yollar."],
                 ["Yüksək yük", "Sorğu limiti, icmalın mikro-keşi, marşrut keşi, SQLite WAL, məhdud SSE növbələri."],
                 ["Təhlükəsizlik tələbi", "JWT, PBKDF2, RBAC, Pydantic validasiyası, təhlükəsizlik başlıqları və CSP, "
                                          "gövdə limiti, audit jurnalı, parametrli SQL."],
             ],
             [4.2, 12.3]),
            ("drops",),
        ],
    },
    {
        "title": "10. Məhdudiyyətlər",
        "blocks": [
            ("bullets", [
                "Məlumat simulyasiya olunub. Yol şəbəkəsi əl ilə qurulmuş qrafdır (36 qovşaq, 62 yol seqmenti); adlar "
                "Bakının real küçə və metro stansiyalarından götürülüb, koordinatlar və uzunluqlar təxminidir. "
                "25 toplanış məntəqəsi (8-i ehtiyat), 8 briqada və 8 rayon haqqında məlumat nümayiş məqsədlidir.",
                "Xarici Xəritə və Marşrutlaşdırma API-si simulyatordur.",
                "Marşrut həndəsəsi qovşaqlar arasında düz parçalardan ibarətdir.",
                "Sorğu limiti, hadisə şini və marşrut keşi proses yaddaşındadır — sistem hazırkı halda tək proseslə işləyir.",
                "SQLite tək yazıçı ilə işləyir; şəhər miqyaslı yük üçün nəzərdə tutulmayıb.",
                "Vətəndaşın kimliyi anonim cihaz identifikatorudur və təsdiqlənmir.",
                "Xəritə fonu (tile) internetdən yüklənir; tətbiq TLS-i özü təmin etmir.",
                "Briqadaların hərəkəti izlənilmir — yalnız status və təyinat nöqtəsi saxlanılır.",
            ]),
        ],
    },
    {
        "title": "11. Gələcək inkişaf imkanları",
        "blocks": [
            ("bullets", [
                "Üfüqi miqyaslanma: sorğu limiti, hadisə şini və keş üçün Redis; PostgreSQL / PostGIS-ə keçid.",
                "Təsdiqlənmiş kimlik: ASAN Login və ya SMS OTP.",
                "Real xəritə məlumatı: OpenStreetMap həndəsəsi və real marşrutlaşdırma mühərriki.",
                "Bildiriş kanalları: push bildirişlər, SMS, oflayn işləyən PWA.",
                "Real məlumat axınları: yol hərəkəti, məntəqələrdə sayğac və sensor məlumatı, briqadaların GPS izlənməsi.",
                "İzdihamın və doluluğun maşın öyrənməsi ilə proqnozlaşdırılması.",
            ]),
        ],
    },
]

# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------
FONT = "Calibri"
ACCENT = RGBColor(0x1F, 0x3A, 0x5F)
HEADER_FILL = "1F3A5F"
ZEBRA_FILL = "F2F5F9"


def _set_font(style, size: float, bold: bool | None = None, color: RGBColor | None = None) -> None:
    style.font.name = FONT
    style.font.size = Pt(size)
    if bold is not None:
        style.font.bold = bold
    if color is not None:
        style.font.color.rgb = color
    rpr = style.element.get_or_add_rPr()
    rfonts = rpr.find(qn("w:rFonts"))
    if rfonts is None:
        rfonts = OxmlElement("w:rFonts")
        rpr.append(rfonts)
    for attr in ("w:ascii", "w:hAnsi", "w:cs", "w:eastAsia"):
        rfonts.set(qn(attr), FONT)
    for theme_attr in ("w:asciiTheme", "w:hAnsiTheme", "w:cstheme", "w:eastAsiaTheme"):
        if rfonts.get(qn(theme_attr)) is not None:
            del rfonts.attrib[qn(theme_attr)]


def _setup_styles(doc: Document) -> None:
    normal = doc.styles["Normal"]
    _set_font(normal, 11)
    normal.paragraph_format.space_after = Pt(6)
    normal.paragraph_format.line_spacing = 1.1
    lang = OxmlElement("w:lang")
    lang.set(qn("w:val"), "az-Latn-AZ")
    normal.element.get_or_add_rPr().append(lang)

    _set_font(doc.styles["Heading 1"], 15, True, ACCENT)
    doc.styles["Heading 1"].paragraph_format.space_before = Pt(14)
    doc.styles["Heading 1"].paragraph_format.space_after = Pt(6)
    doc.styles["Heading 1"].paragraph_format.keep_with_next = True
    _set_font(doc.styles["Heading 2"], 12, True, ACCENT)
    doc.styles["Heading 2"].paragraph_format.space_before = Pt(10)
    doc.styles["Heading 2"].paragraph_format.space_after = Pt(4)
    doc.styles["Heading 2"].paragraph_format.keep_with_next = True
    _set_font(doc.styles["List Bullet"], 11)
    doc.styles["List Bullet"].paragraph_format.space_after = Pt(3)

    for section in doc.sections:
        section.page_width, section.page_height = Cm(21.0), Cm(29.7)  # A4
        section.top_margin = section.bottom_margin = Cm(2.0)
        section.left_margin = section.right_margin = Cm(2.2)


def _shade(cell, fill: str) -> None:
    tc_pr = cell._tc.get_or_add_tcPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), fill)
    tc_pr.append(shd)


def _cell_text(cell, text: str, bold: bool = False, color: RGBColor | None = None) -> None:
    para = cell.paragraphs[0]
    para.paragraph_format.space_after = Pt(2)
    para.paragraph_format.space_before = Pt(2)
    para.paragraph_format.line_spacing = 1.0
    run = para.add_run(text)
    run.font.size = Pt(10)
    run.bold = bold
    if color is not None:
        run.font.color.rgb = color


def _add_table(doc: Document, headers: list[str], rows: list[list[str]], widths: list[float] | None) -> None:
    table = doc.add_table(rows=1, cols=len(headers))
    table.style = "Table Grid"
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    table.autofit = False

    header_row = table.rows[0]
    tr_pr = header_row._tr.get_or_add_trPr()
    repeat = OxmlElement("w:tblHeader")  # başlıq sətri hər səhifədə təkrarlanır
    repeat.set(qn("w:val"), "true")
    tr_pr.append(repeat)
    for cell, text in zip(header_row.cells, headers):
        _cell_text(cell, text, bold=True, color=RGBColor(0xFF, 0xFF, 0xFF))
        _shade(cell, HEADER_FILL)

    for i, row in enumerate(rows):
        cells = table.add_row().cells
        for j, (cell, text) in enumerate(zip(cells, row)):
            _cell_text(cell, text, bold=(j == 0))
            if i % 2 == 1:
                _shade(cell, ZEBRA_FILL)

    if widths:
        for column, width in zip(table.columns, widths):  # tblGrid (LibreOffice bunu oxuyur)
            column.width = Cm(width)
        for row in table.rows:                            # hüceyrə enləri (Word bunu oxuyur)
            for cell, width in zip(row.cells, widths):
                cell.width = Cm(width)
    doc.add_paragraph().paragraph_format.space_after = Pt(2)


def _add_drops(doc: Document) -> None:
    if not CHALLENGE_DROPS:
        doc.add_paragraph(
            "Konkret Challenge Drop-lar elan olunduqca hər biri üçün tələb, həll və dəyişdirilən fayllar bu bölməyə "
            "əlavə olunur.")
        return
    doc.add_paragraph("Müsabiqə zamanı elan olunan dəyişikliklər və onlara verilən cavab:")
    for drop in CHALLENGE_DROPS:
        doc.add_heading(drop["title"], level=2)
        for text in drop.get("paragraphs", []):
            doc.add_paragraph(text)


def _add_title(doc: Document) -> None:
    def line(text: str, size: float, bold: bool = False, color: RGBColor | None = None, after: float = 4) -> None:
        para = doc.add_paragraph()
        para.alignment = WD_ALIGN_PARAGRAPH.CENTER
        para.paragraph_format.space_after = Pt(after)
        run = para.add_run(text)
        run.font.size = Pt(size)
        run.bold = bold
        if color is not None:
            run.font.color.rgb = color

    doc.add_paragraph().paragraph_format.space_after = Pt(18)
    line(TITLE["project"], 24, True, ACCENT)
    line(TITLE["subtitle"], 13, False, RGBColor(0x55, 0x55, 0x55), after=14)
    line(TITLE["doc_type"], 18, True, after=14)
    line(f"Tarix: {TITLE['date']}", 11)
    line(TITLE["team"], 11, after=10)

    rule = doc.add_paragraph()
    p_pr = rule._p.get_or_add_pPr()
    borders = OxmlElement("w:pBdr")
    bottom = OxmlElement("w:bottom")
    for key, value in (("w:val", "single"), ("w:sz", "8"), ("w:space", "1"), ("w:color", HEADER_FILL)):
        bottom.set(qn(key), value)
    borders.append(bottom)
    p_pr.append(borders)


def build(output: Path = OUTPUT) -> Path:
    doc = Document()
    _setup_styles(doc)
    doc.core_properties.title = f"{TITLE['project']} — {TITLE['doc_type']}"
    doc.core_properties.language = "az-Latn-AZ"
    _add_title(doc)

    for section in SECTIONS:
        doc.add_heading(section["title"], level=1)
        for block in section["blocks"]:
            kind = block[0]
            if kind == "h2":
                doc.add_heading(block[1], level=2)
            elif kind == "p":
                doc.add_paragraph(block[1])
            elif kind == "bullets":
                for item in block[1]:
                    doc.add_paragraph(item, style="List Bullet")
            elif kind == "table":
                _add_table(doc, block[1], block[2], block[3] if len(block) > 3 else None)
            elif kind == "drops":
                _add_drops(doc)
            else:
                raise ValueError(f"Naməlum blok növü: {kind}")

    output.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(output))
    return output


if __name__ == "__main__":
    path = build()
    check = Document(str(path))
    print(f"Yaradıldı: {path}")
    print(f"Abzas: {len(check.paragraphs)}, cədvəl: {len(check.tables)}, Challenge Drop: {len(CHALLENGE_DROPS)}")
