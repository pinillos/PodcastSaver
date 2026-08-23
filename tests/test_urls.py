from podcast_kb.urls import enclosure_sha256, normalize_enclosure_url, unwrap_trackers

REAL = "https://traffic.megaphone.fm/ABC1234567890.mp3"


class TestUnwrapTrackers:
    def test_podtrac_redirect_sin_esquema_interno(self):
        url = "https://dts.podtrac.com/redirect.mp3/traffic.megaphone.fm/ABC1234567890.mp3"
        assert unwrap_trackers(url) == REAL

    def test_podtrac_pts_redirect(self):
        url = "https://www.podtrac.com/pts/redirect.mp3/traffic.megaphone.fm/ABC1234567890.mp3"
        assert unwrap_trackers(url) == REAL

    def test_chartable_con_esquema_interno(self):
        url = "https://chtbl.com/track/1A2B3C/traffic.megaphone.fm/ABC1234567890.mp3"
        assert unwrap_trackers(url) == REAL

    def test_prefijos_encadenados(self):
        url = (
            "https://pdst.fm/e/chtbl.com/track/1A2B3C/"
            "dts.podtrac.com/redirect.mp3/traffic.megaphone.fm/ABC1234567890.mp3"
        )
        assert unwrap_trackers(url) == REAL

    def test_url_sin_tracker_no_se_toca(self):
        assert unwrap_trackers(REAL) == REAL

    def test_anchor_no_es_tracker(self):
        url = "https://anchor.fm/s/e1671d44/podcast/play/12345/episode.mp3"
        assert unwrap_trackers(url) == url


class TestNormalize:
    def test_quita_query_de_analitica(self):
        url = REAL + "?dest-id=99887&updated=1721030400&awCollectionId=42"
        assert normalize_enclosure_url(url) == REAL

    def test_quita_fragmento(self):
        assert normalize_enclosure_url(REAL + "#t=120") == REAL

    def test_normaliza_host_y_esquema(self):
        url = "HTTPS://Traffic.Megaphone.FM/ABC1234567890.mp3"
        assert normalize_enclosure_url(url) == REAL

    def test_puerto_por_defecto_se_omite(self):
        assert normalize_enclosure_url("https://traffic.megaphone.fm:443/ABC1234567890.mp3") == REAL

    def test_puerto_no_estandar_se_conserva(self):
        url = "https://ejemplo.com:8443/ep.mp3"
        assert normalize_enclosure_url(url) == url

    def test_el_path_distingue_episodios(self):
        a = normalize_enclosure_url("https://traffic.megaphone.fm/EP1.mp3")
        b = normalize_enclosure_url("https://traffic.megaphone.fm/EP2.mp3")
        assert a != b


class TestDedupeGlobal:
    def test_mismo_audio_en_dos_feeds_con_tracking_distinto(self):
        """El caso de §2.2: La Tertul-IA publicada también dentro de Growth.

        Dos feeds, dos prefijos de tracking distintos, dos guid distintos —
        pero el mismo fichero de audio detrás.
        """
        feed_tertulia = (
            "https://chtbl.com/track/TERTULIA/traffic.megaphone.fm/EPISODIO42.mp3?dest-id=111"
        )
        feed_growth = (
            "https://dts.podtrac.com/redirect.mp3/traffic.megaphone.fm/EPISODIO42.mp3?dest-id=222"
        )
        assert enclosure_sha256(feed_tertulia) == enclosure_sha256(feed_growth)

    def test_episodios_distintos_no_colisionan(self):
        assert enclosure_sha256("https://h.com/a.mp3") != enclosure_sha256("https://h.com/b.mp3")
