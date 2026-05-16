"""Stop words for word cloud filtering.

Russian words only need to be listed in their BASE (nominative/infinitive) form
because pymorphy2 lemmatises every token before the check.  All declined and
conjugated forms (e.g. "москвы", "выборах", "говорили") are normalised to their
base before lookup, so listing them here is redundant.

English words are not lemmatised — list every surface form that should be
suppressed (past tense, plurals, etc.) when it matters.
"""

STOP_WORDS: frozenset[str] = frozenset({
    # ── English — function words ──────────────────────────────────────────────
    "the", "be", "to", "of", "and", "a", "in", "that", "have", "it",
    "for", "not", "on", "with", "he", "as", "you", "do", "at", "this",
    "but", "his", "by", "from", "they", "we", "say", "her", "she", "or",
    "an", "will", "my", "one", "all", "would", "there", "their", "what",
    "so", "up", "out", "if", "about", "who", "get", "which", "go", "me",
    "when", "make", "can", "like", "time", "no", "just", "him", "know",
    "take", "people", "into", "year", "your", "good", "some", "could",
    "them", "see", "other", "than", "then", "now", "look", "only", "come",
    "its", "over", "think", "also", "back", "after", "use", "two", "how",
    "our", "work", "first", "well", "way", "even", "new", "want", "any",
    "these", "give", "day", "most", "us", "been", "has", "was", "were",
    "are", "had", "did", "said", "each", "more", "very", "may", "still",
    "through", "where", "while", "those", "though", "too", "much", "off",
    "here", "both", "between", "need", "large", "often", "hand", "high",
    "place", "hold", "such", "again", "own", "three", "must", "big", "far",
    "down", "don", "put", "same", "got", "let", "great", "few",
    "before", "never", "always", "every", "little", "actually", "really",
    "because",
    # ── English — URL / tech fragments appearing in post bodies ──────────────
    "http", "https", "www", "com", "org", "net", "html", "htm", "php",
    "address", "facebook", "twitter",
    # ── Russian — core function words (base forms; pymorphy2 covers all cases) ─
    # Prepositions
    "в", "на", "с", "по", "за", "от", "до", "из", "к", "у", "о", "об",
    "при", "без", "над", "под", "про", "для", "через", "между", "вокруг",
    "перед", "после", "около", "среди", "кроме", "вместо", "вдоль",
    "внутри", "против",  # Note: "против" kept as content word below — remove if needed
    # Conjunctions / particles
    "и", "а", "но", "или", "что", "как", "если", "то", "же", "ли", "бы",
    "ни", "не", "ни", "да", "нет", "уже", "ещё", "еще", "даже", "вот",
    "тоже", "также", "лишь", "только", "хоть", "хотя", "чтоб", "чтобы",
    "зато", "либо", "однако", "ведь", "ну", "вдруг", "вообще",
    # Pronouns (base forms — pymorphy2 maps all cases here)
    "я", "ты", "он", "она", "оно", "мы", "вы", "они",
    "мой", "твой", "свой", "наш", "ваш", "их",
    "этот", "тот", "такой", "который", "весь", "каждый", "любой",
    "сам", "себя", "кто", "что", "никто", "ничто", "некто", "нечто",
    "некоторый", "несколько",
    # ── Russian — быть and other auxiliary verbs ─────────────────────────────
    "быть", "стать", "являться",
    # ── Russian — discourse / modal / filler words (base forms) ──────────────
    "очень", "довольно", "почти", "немного", "примерно", "вполне",
    "совсем", "слишком", "весьма", "достаточно", "чуть", "скорее",
    "сразу", "раньше", "позже", "хорошо", "плохо", "лучше", "хуже",
    "полностью", "абсолютно", "реально", "сильно", "дальше", "вместе",
    "важно", "никогда", "обычно", "совершенно", "действительно",
    "иногда", "иначе", "внезапно", "непонятно", "осторожно",
    "наверняка", "сначала", "скоро", "похоже", "крайне", "поздно",
    "давно", "лично", "гораздо", "назад", "вперёд", "вперед",
    "кстати", "например", "именно", "конечно", "обязательно",
    "просто", "прямо", "поэтому", "потом", "теперь", "сегодня",
    "всегда", "нельзя", "можно", "нужно", "должен", "надо",
    "нибудь", "итак", "пожалуй", "видимо", "наверное", "наверно",
    "кажется", "вроде", "короче", "вместо", "равно", "наконец",
    "опять", "снова", "особенно", "точно", "правильно", "понятно",
    "интересно", "ведь", "мол", "дескать", "якобы", "буквально",
    "давайте", "спасибо", "пожалуйста", "правда", "тут",
    "собственно", "возможно", "значит", "пока",
    # ── Russian — filler phrase fragments (preposition complements) ───────────
    # These only appear as parts of frozen phrases ("таким образом",
    # "в связи с", "в качестве", "к сожалению", "в том числе", "в общем")
    # and are noise on their own.
    "образом", "связи", "качестве", "сожалению", "числе", "общем", "конце",
    # ── Russian — common generic verbs (infinitive = pymorphy2 base form) ────
    # Imperfective
    "делать", "работать", "хотеть", "стоить",
    "говорить", "сказать", "знать", "думать", "жить",
    "смотреть", "читать", "получить", "найти", "искать",
    "использовать", "купить", "рассказывать",
    "оказаться", "понять", "видеть",
    "считать", "понимать", "писать", "иметь", "казаться",
    "начинать", "идти", "мочь",
    # Perfective counterparts not yet covered above
    "начать", "пройти", "смочь", "сделать", "решить",
    "прийти", "написать", "выйти", "дать",
    # ── Russian — evaluative / generic adjectives (base forms) ───────────────
    "хороший", "новый", "другой", "самый", "прекрасный", "отличный",
    "главный", "крутой", "большой", "последний", "смешной", "забавный",
    "первый", "второй", "третий",
    "нормальный", "больший", "настоящий", "должный", "общий",
    "нужный", "единственный", "важный", "простой", "плохой",
    "остальной", "маленький",
    # ── Russian — generic nouns (base forms) ─────────────────────────────────
    "человек", "люди",
    "случай", "часть", "место", "вопрос", "дело", "тема",
    "пост", "тип", "слово", "история", "результат",
    "жизнь", "путь", "время", "страна", "новость",
    "месяц", "тысяча", "тыщ",
    "сайт", "вещь", "количество",
    "пара",      # "пару" → "пара" after lemmatisation
    "раз",       # "раза" → "раз"
    "год", "день",
    "ситуация", "качество", "минута", "процесс", "конец",
    "час", "возможность", "сторона", "ссылка", "система",
    # ── Russian — pronouns / determiners not caught by lemmatiser ────────────
    "всё", "это", "никакой",
    # ── Russian — question / filler words ────────────────────────────────────
    "сколько", "почему",
    # ── Russian — evaluative interjections ───────────────────────────────────
    "молодец", "круто",
    # ── Russian — courtesy / call-to-action ──────────────────────────────────
    "приходить", "почитать", "посмотреть", "рекомендовать",
    "надеяться", "читать", "давать",
    # ── Russian — misc filler / noise ────────────────────────────────────────
    "чувак",
    "нибыть",    # tokenisation artifact (ни + быть run together)
})
