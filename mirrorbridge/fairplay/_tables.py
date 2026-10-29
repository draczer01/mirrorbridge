# MirrorBridge's FairPlay SAP: a Python port of doubletake's Go implementation
# (https://github.com/omarroth/doubletake, internal/airplay/fpsap_tables.go and
# fairplay_message.go). Copyright the doubletake authors; this module is free software under
# the GNU Lesser General Public License version 3 (LICENSE and COPYING.GPL in this folder).
"""The constant data of FairPlay SAP: the white-box tables and the message cipher's keys."""

# The fixed sixteen-byte block both white-box networks start from.
FIXED_BLOCK = bytes.fromhex("afc22ba049effcfbfe67ac5ebef6fbcb")
FIRST_POSITION_MAP = (0, 5, 10, 15, 4, 9, 14, 3, 8, 13, 2, 7, 12, 1, 6, 11)
SECOND_POSITION_MAP = (0, 13, 10, 7, 4, 1, 14, 11, 8, 5, 2, 15, 12, 9, 6, 3)
M1_PAYLOAD = bytes((0x02, 0x00, 0x03, 0xBB))  # 3: every capability; the receiver picks the mode in m2
M3_LABEL = bytes((0x8F, 0x1A, 0x9C))
DESCRIPTOR_PREFIX = bytes.fromhex("a0449c4d09e4bd7f6ec5d0cc359da7467a")
DESCRIPTOR_SUFFIX = bytes.fromhex("97b50f84e2155a9c24991cf43a09635547")
MASK_STATE = (0x1D4A4587, 0x92F39FCC, 0x1D87D836, 0xCDC86697)
MASK_SUFFIX = bytes.fromhex("57d8eecbdefbcf591c27a2cfbeb089")
DIGEST32_STATE = (0xB9F3DCDC, 0xFBDC740B, 0x60F77F86, 0x51907216)

INITIAL_SESSION_KEY = bytes.fromhex("dcdcf3b90b74dcfb867ff76016729051")
KDF_PREFIX = bytes.fromhex("fa9cad4d4b68268c7ff38899de922e951e")
KDF_SUFFIX = bytes.fromhex("ec4e275efdf2e83097ae70fbe0003f1c39")
EKEY_HEADER = b"FPLY" + bytes((0x01, 0x02, 0x01, 0x00, 0x00, 0x00, 0x00, 0x3C, 0x00, 0x00, 0x00, 0x00))

# The SAP hash's initial state.
SAP_HASH = bytes.fromhex("965fc653f846cc18dfbeb2f83862ec2293d1208f")
SAP_MATRIX = bytes.fromhex("4354627a18c3d6b39a56f61c143f0c1d3b3683b139514aaa093efe44afdec3209d42b8")
SAP_SEED = bytes.fromhex("ed25d1bbbc279f02a2a911000cb352c0bde31b49c7")

MESSAGE_IV = tuple(bytes.fromhex(h) for h in (
    "5752f1b7549d8f870c10485a6088cadb",
    "df7b1563f005587752a90402b9a39295",
    "68b54611fb04de676c968efb8c9db0c9",
    "27078b2123361e7adc9d0b115354690d",
))
MESSAGE_ROUND_KEY_0 = bytes.fromhex("d30dfdb563def72b012ead72884dca25")
MESSAGE_ROUND_KEY_10 = bytes.fromhex("cd5ecf47e9af289b51188f68d085eb69")
# The nine middle round keys of each of the four modes.
MESSAGE_MIDDLE_KEYS = tuple(
    tuple(bytes.fromhex(h) for h in keys) for keys in (
        (
            "6af008a6ae5118268acccb4357d1e711",
            "c6b579e0dfce8114d41d91c868513be3",
            "bafc24065ebc9cf6131652de75afd0de",
            "81a8b516836ee6493cccc125db99e0ec",
            "781b6ceb300cfe8f3c0c54c92dc7e2a3",
            "594cc871ad4a3dd851a884b0ed048f5c",
            "afea245eae1f2de557bb511b17ddcc3d",
            "8e7a945569c09856533872ca6a3a00de",
            "f3ca31810cb6201b24b5c7e4ee108bc0",
        ),
        (
            "1b9018bea49613e86ab413627c84a766",
            "c94c1dc55c8bbb04a940f640f420eba5",
            "d5664239df63981372b7a8b2360b160b",
            "462cd7af87b8005a47157e32644557cf",
            "f0c0ac26f01988a8573d39e298f97f6a",
            "9407abf947788ceada25ee558333edb6",
            "3f820f9ed823ce2f7722c4bab1d2e335",
            "71fca0ff354f8991109961878bfa7cfb",
            "566c98381eedcfe7954f7213f61fc87e",
        ),
        (
            "2ab2ebe11496a9655dd1b28a9910ee30",
            "9cbc01d64fa41ca1f705a0ef7fb47934",
            "08f02908920c5b348d32d3470b07705b",
            "8735404470486fa4a42b04fbfde11e3c",
            "d8eba4780f69653dfc7d2b4fbfb1ac68",
            "a74a7a6696be42e1b884e1acff1311a4",
            "cc7a007cd8a3f330d3f17ba3437d52cd",
            "1edeff958a6254b20114cfddc02a48f8",
            "3a36f4e069e1907b2c8c779be7336c09",
        ),
        (
            "f6a223b4f01d8a7927a7c965de554de0",
            "0cd1515d43246c87fc2f494364a4cfbc",
            "d66e3bdd6c7f1b811f90291984b5eaee",
            "530d7ae85b3040569f13130f18fbb1e5",
            "0784c47a9fe10f3d5ea5432ecdd9f585",
            "b9b0368eb7d134d90ebd1b94b255a4fb",
            "b2a4352534066fdec1ce38db3d8c3b85",
            "0b317a942520622d89578e9fe5a13fc3",
            "bdf569e6befc1d2fc08baf733f1d176a",
        ),
    )
)
INVERSE_SBOX = bytes.fromhex(
    "52096ad53036a538bf40a39e81f3d7fb7ce339829b2fff87348e4344c4dee9cb"
    "547b9432a6c2233dee4c950b42fac34e082ea16628d924b2765ba2496d8bd125"
    "72f8f66486689816d4a45ccc5d65b6926c704850fdedb9da5e154657a78d9d84"
    "90d8ab008cbcd30af7e45805b8b34506d02c1e8fca3f0f02c1afbd0301138a6b"
    "3a9111414f67dcea97f2cfcef0b4e67396ac7422e7ad3585e2f937e81c75df6e"
    "47f11a711d29c5896fb7620eaa18be1bfc563e4bc6d279209adbc0fe78cd5af4"
    "1fdda8338807c731b11210592780ec5f60517fa919b54a0d2de57a9f93c99cef"
    "a0e03b4dae2af5b0c8ebbb3c83539961172b047eba77d626e169146355210c7d"
)

FIRST_INPUT_MASK = bytes.fromhex("a09675fafe91ea7be48a79df6e71dd17")
SECOND_OUTPUT_MASK = bytes.fromhex("67bc54c08e32851b50d2125f68b740a5")

# Six substitution bases and seven mix bases, 256 bytes each, end to end.
_SUBSTITUTION = bytes.fromhex(
    "491d7615f23ab6ac34a0697f81d788bb716e2816be7038ba9801a65b92ff208e"
    "d0b00ec92fde3d6f9f53b7fcc87b8264feda5c0bf386b24236d6f5f8e92aef83"
    "cb9a8d12dde76558875900cafd8bf0844fb1bda1a585d19d547355f137c3d206"
    "5626d85033f79ba41475cd3ba905676be6625f66bf03cfc15d4ac0aa45729e8a"
    "22948fb4890948174002fa60eeabb8e3e13ec2c463e4748c46b90fd9f41c4447"
    "eb9141fb182da3a87a903f4dad436a937e116de29932d5b31edb31e85eec3996"
    "51b5af9761a7c613682cf627e55a77ce0a233c309c190d2e0824f9104e7cd407"
    "c735d38095edbcae29e0dca26c2b21794cc52552cc571b4b0c1f1a04ea7ddf78"
    "576b2d8364328aedfe3f561adce631d0fc11aa35c53b91f821bf392ff36a33df"
    "a3e91ba945bcc8d5501f0b90dbc9e0665dd39f6d41362514ec6f2bc1a47d4243"
    "0e1e5f290cd87a5cae72b37b2aa8054bea9badb23d5ecf6cd6fb7552804ae253"
    "62a2b0e84dda04269982959e67ccef18938678f70379de96cd4cf5eb7eb7c7c0"
    "8e098419a706e4bbf9cbfa71490f4ff6b5f13e7f5b94ca23f244a577d4ddb8be"
    "fd0d92487481c63702162412a12c15b9c234af54873c988f9d5aac1cabba7c58"
    "68bd467628408d08d78cc3132e8b17ff38ee654ee5d1f4b63059b401a0c47397"
    "d91d27b19a106e60d261886389209cf0e70770e3473ace8522a60a00555169e1"
    "faf955718844caf7389801b7585799489d8f26e983bc43dd1e0e543642d3eb9b"
    "35ec053c2fffb26cfb635fe5768e29d5b8cb652b141f18d8e01d61c97a399c4a"
    "80db5bb4c5f212502d4d41cf172c282a2730246e22c20aaf3ea4fc40847f7252"
    "c85cfe7d03f5a83ac034c7df68a07bdc568da15eee0f678c15643259a5f191ea"
    "a7e8fd82de60789f930870966d0c3d9089513be48779335dd7f8b06a23b61020"
    "e64cf4b5f37ec16246edc63f37d4d6d0ce495a0dbbe7026b07b3c49ab99e75ae"
    "be9453a33147a6161bbd6f00adaa77731cd2b1d9f04f744eef66d195e285bf92"
    "4b97a98aa2137c861921f6abe30611ccba8bcd25ace109040b81451a69da2ec3"
    "ab0db5c89ded1150638b8379127c8d23a1eb4ce6d2537b62f01428ae9eafb68c"
    "c6b38540946d3258f7f886b1dddbfc375ba4ee46271a419389df035a598760d5"
    "e5b7c13cce1fc26f36095fe010524a614be742bea0de2da99121fe39b92668bb"
    "25ec4f970a4efb81495c67c71c17b8a716adf969e43e00d9ac3d2e47d1741d54"
    "079bd35629dca6c9339c2afd72f50b7d228096043fe92f34658438a32be124c4"
    "7f57c0b4eaca45999075bc71b0f61bcb9f06ff9820d4fa9ad08a1870b25e133a"
    "e3355d1ed8d6da51026608d7cff1302c6b95aaf2bac3ef780c8fa24d44437a77"
    "15a8c5f305f40e0f5548bfe2bd927e6ccd82313bcc196e6a01767364e88ea588"
    "fa3b8755c613dc89b8c767c9bc822844afdf77d40549f67d07d9caf9e1082904"
    "aed865ad63c223617f1f7e027c79cf1560f54ba758112a9d390c56a312bf699a"
    "6a7b2d2734cb99f43e800ed19078e6e476a06ef32b33413c3d4a837350a6421b"
    "b40b96184803e8475dcdaa8616bd62fbbb326ffee78f1dffb7a40d5fd752946c"
    "71c8b2256decd6a535a1d2c046401c20be4feeb9f0eabad0383f8a2e0f218cb6"
    "5c43d5f2684df7b157d3fcdac5fd8e66abe317887292ac3a018dc18524f8f1ef"
    "599764ce8b759b5b510a3700dec3817a9eebe02c9c1e9fa906e2cc705e26b55a"
    "09c4eddda21a9184b0224e2f109845e9e54c74959354db36a83031196bb35314"
    "5467c51a5309a6ebac07c2e76ca78d7bc1832a065121b73652e67414998e0f17"
    "554f48350ce33f2e4297821f840e4d7fe481c3bfa1a9af1c57d4287eb4186641"
    "cc95457090e98fb3e5bd3e8c3cbba0fc3a9c65ca5b4e34044c2b1d30da3ba38a"
    "f193e0c6777a5d926af476cf232c404bb6de0d395620e25e44eafe259b62b931"
    "f2d9cb61ee1ed180a5c924d7e812ed3de1085c38bad364b526d2aa91be166b05"
    "6dfbdf5f194a7589d6f89a2285d833d003688713b1dbc743019df9b8d5a4f6fa"
    "462f6e73c48b9471b2ec27110ac86fae6996ce021015597dfddd0bb0efff2da8"
    "7c9e9f98a288ad37abf050dc0086f7795a58f560c06329f3783272bc49471bcd"
)
_MIX = bytes.fromhex(
    "46ceb139ba324dc558d0af27a42c53db870f70f87bf38c0499116ee665ed921a"
    "ea621d95169ee169f47c038b0880ff772ba3dc54d75f20a835bdc24ac9413eb6"
    "cf4738b033bbc44cd15926ae2da5da520e86f971f27a058d1098e76fec641b93"
    "63eb941c9f1768e07df58a02810976fea22a55dd5ed6a921bc344bc340c8b73f"
    "3cb4cb43c04837bf22aad55dde5629a1fd750a820189f67ee36b149c1f97e860"
    "901867ef6ce49b138e0679f172fa850d51d9a62ead255ad24fc7b830b33b44cc"
    "b53d42ca49c1be36ab235cd457dfa02874fc830b88007ff76ae29d15961e61e9"
    "1991ee66e56d129a078ff078fb730c84d8502fa724acd35bc64e31b93ab2cd45"
    "fbfaf9f8fffefdfcf3f2f1f0f7f6f5f4ebeae9e8efeeedece3e2e1e0e7e6e5e4"
    "dbdad9d8dfdedddcd3d2d1d0d7d6d5d4cbcac9c8cfcecdccc3c2c1c0c7c6c5c4"
    "bbbab9b8bfbebdbcb3b2b1b0b7b6b5b4abaaa9a8afaeadaca3a2a1a0a7a6a5a4"
    "9b9a99989f9e9d9c93929190979695948b8a89888f8e8d8c8382818087868584"
    "7b7a79787f7e7d7c73727170777675746b6a69686f6e6d6c6362616067666564"
    "5b5a59585f5e5d5c53525150575655544b4a49484f4e4d4c4342414047464544"
    "3b3a39383f3e3d3c33323130373635342b2a29282f2e2d2c2322212027262524"
    "1b1a19181f1e1d1c13121110171615140b0a09080f0e0d0c0302010007060504"
    "af265ad357dea22bb9304cc541c8b43d7ef78b02860f73fa68e19d14901965ec"
    "23aad65fdb522ea735bcc049cd4438b1f27b078e0a83ff76e46d11981c95e960"
    "66ef931a9e176be270f9850c88017df4b73e42cb4fc6ba33a12854dd59d0ac25"
    "ea631f96129be76efc750980048df1783bb2ce47c34a36bf2da4d851d55c20a9"
    "55dca029ad2458d143cab63fbb324ec7840d71f87cf58900921b67ee6ae39f16"
    "d9502ca521a8d45dcf463ab337bec24b0881fd74f079058c1e97eb62e66f139a"
    "9c1569e064ed91188a037ff672fb870e4dc4b831b53c40c95bd2ae27a32a56df"
    "1099e56ce8611d94068ff37afe770b82c14834bd39b0cc45d75e22ab2fa6da53"
    "0134407587b2c6f30530447183b6c2f73e0b7f4ab88df9cc3a0f7b4ebc89fdc8"
    "083d497c8ebbcffa0c394d788abfcbfe37027643b184f0c533067247b580f4c1"
    "d9ec98ad5f6a1e2bdde89ca95b6e1a2fe6d3a79260552114e2d7a39664512510"
    "d0e591a456631722d4e195a052671326efdaae9b695c281debdeaa9f6d582c19"
    "1c295d689aafdbee182d596c9eabdfea23166257a590e4d127126653a194e0d5"
    "1520546193a6d2e71124506597a2d6e32a1f6b5eac99edd82e1b6f5aa89de9dc"
    "c4f185b042770336c0f581b446730732fbceba8f7d483c09ffcabe8b794c380d"
    "cdf88cb94b7e0a3fc9fc88bd4f7a0e3bf2c7b38674413500f6c3b78270453104"
    "6fb7c61e6db5c41ca97100d8ab7302dafa22538bf82051893ce4954d3ee6974f"
    "de0677afdc0475ad18c0b1691ac2b36b4b93e23a4991e0388d5524fc8f5726fe"
    "924a3be3904839e1548cfd25568eff2707dfae7605ddac74c11968b0c31b6ab2"
    "23fb8a5221f98850e53d4c94e73f4e96b66e1fc7b46c1dc570a8d90172aadb03"
    "855d2cf4875f2ef6439bea324199e83010c8b96112cabb63d60e7fa7d40c7da5"
    "34ec9d4536ee9f47f22a5b83f0285981a17908d0a37b0ad267bfce1665bdcc14"
    "78a0d1097aa2d30bbe6617cfbc6415cded35449cef37469e2bf3825a29f18058"
    "c91160b8cb1362ba0fd7a67e0dd5a47c5c84f52d5e86f72f9a4233eb984031e9"
    "ba060eb2c47870cca8141ca0d66a62de54e8e05c2a969e2246faf24e38848c30"
    "3f838b3741fdf5492d91992553efe75bd16d65d9af131ba7c37f77cbbd0109b5"
    "ab171fa3d56961ddb9050db1c77b73cf45f9f14d3b878f3357ebe35f29959d21"
    "2e929a2650ece4583c80883442fef64ac07c74c8be020ab6d26e66daac1018a4"
    "5de1e955239f972b4ff3fb47318d8539b30f07bbcd7179c5a11d15a9df636bd7"
    "d8646cd0a61a12aeca767ec2b40800bc368a823e48f4fc402498902c5ae6ee52"
    "4cf0f844328e863a5ee2ea56209c9428a21e16aadc6068d4b00c04b8ce727ac6"
    "c9757dc1b70b03bfdb676fd3a51911ad279b932f59e5ed513589813d4bf7ff43"
    "7c2c227282d2dc8ca4f4faaa5a0a045428787626d68688d8f0a0aefe0e5e5000"
    "61313f6f9fcfc191b9e9e7b74717194935656b3bcb9b95c5edbdb3e313434d1d"
    "08585606f6a6a8f8d0808ede2e7e70205c0c0252a2f2fcac84d4da8a7a2a2474"
    "15454b1bebbbb5e5cd9d93c333636d3d41111f4fbfefe1b199c9c79767373969"
    "ecbcb2e212424c1c34646a3aca9a94c4b8e8e6b64616184860303e6e9ecec090"
    "f1a1afff0f5f510129797727d78789d9a5f5fbab5b0b05557d2d237383d3dd8d"
    "98c8c6966636386840101e4ebeeee0b0cc9c92c232626c3c14444a1aeabab4e4"
    "85d5db8b7b2b25755d0d0353a3f3fdadd1818fdf2f7f712109595707f7a7a9f9"
)
SUBSTITUTION_BASES = [_SUBSTITUTION[i * 256:(i + 1) * 256] for i in range(6)]
MIX_BASES = [_MIX[i * 256:(i + 1) * 256] for i in range(7)]

# (base, input XOR, output XOR): 9 rounds of 16, then 4x4 mix columns, then 16 final.
FIRST_ROUNDS = (
    (0, 0x00, 0x00), (0, 0xde, 0xdb), (0, 0x2e, 0x93), (0, 0x66, 0xb2), (0, 0xb0, 0x00), (0, 0x0d, 0xdb), (0, 0x24, 0x93), (0, 0xf8, 0xb2),
    (0, 0xd2, 0x00), (0, 0xfd, 0xdb), (0, 0x7e, 0x93), (0, 0xa1, 0xb2), (0, 0x5b, 0x00), (0, 0x9e, 0xdb), (0, 0x19, 0x93), (0, 0xf6, 0xb2),
    (1, 0x00, 0x00), (1, 0x63, 0xdb), (1, 0x46, 0x93), (1, 0x57, 0xb2), (1, 0xa4, 0x00), (1, 0x73, 0xdb), (1, 0x85, 0x93), (1, 0xcb, 0xb2),
    (1, 0xc9, 0x00), (1, 0x96, 0xdb), (1, 0x36, 0x93), (1, 0x05, 0xb2), (1, 0x80, 0x00), (1, 0xd7, 0xdb), (1, 0x35, 0x93), (1, 0xff, 0xb2),
    (1, 0x61, 0x00), (1, 0x63, 0xdb), (1, 0x06, 0x93), (1, 0xf7, 0xb2), (1, 0x2c, 0x00), (1, 0xa2, 0xdb), (1, 0xfb, 0x93), (1, 0x87, 0xb2),
    (1, 0x0c, 0x00), (1, 0x86, 0xdb), (1, 0xb5, 0x93), (1, 0x39, 0xb2), (1, 0x65, 0x00), (1, 0xe3, 0xdb), (1, 0xf8, 0x93), (1, 0x7d, 0xb2),
    (1, 0x2a, 0x00), (1, 0x21, 0xdb), (1, 0xa1, 0x93), (1, 0x5e, 0xb2), (1, 0xef, 0x00), (1, 0x31, 0xdb), (1, 0x22, 0x93), (1, 0x62, 0xb2),
    (1, 0x0a, 0x00), (1, 0x05, 0xdb), (1, 0xef, 0x93), (1, 0xe0, 0xb2), (1, 0x86, 0x00), (1, 0x54, 0xdb), (1, 0x6f, 0x93), (1, 0x26, 0xb2),
    (1, 0xc1, 0x00), (1, 0x4a, 0xdb), (1, 0xed, 0x93), (1, 0x45, 0xb2), (1, 0xc7, 0x00), (1, 0xc9, 0xdb), (1, 0xb7, 0x93), (1, 0x9c, 0xb2),
    (1, 0x24, 0x00), (1, 0x7e, 0xdb), (1, 0x20, 0x93), (1, 0xc7, 0xb2), (1, 0x4b, 0x00), (1, 0x98, 0xdb), (1, 0x37, 0x93), (1, 0x5a, 0xb2),
    (1, 0x22, 0x00), (1, 0xfd, 0xdb), (1, 0x16, 0x93), (1, 0x24, 0xb2), (1, 0x0c, 0x00), (1, 0x86, 0xdb), (1, 0xd9, 0x93), (1, 0x03, 0xb2),
    (1, 0xc1, 0x00), (1, 0x4a, 0xdb), (1, 0x81, 0x93), (1, 0x7f, 0xb2), (1, 0x63, 0x00), (1, 0x60, 0xdb), (1, 0xce, 0x93), (1, 0x9e, 0xb2),
    (1, 0x63, 0x00), (1, 0x86, 0xdb), (1, 0xd3, 0x93), (1, 0x1d, 0xb2), (1, 0x86, 0x00), (1, 0xb2, 0xdb), (1, 0x72, 0x93), (1, 0xa5, 0xb2),
    (1, 0xae, 0x00), (1, 0x4a, 0xdb), (1, 0x8b, 0x93), (1, 0x61, 0xb2), (1, 0x24, 0x00), (1, 0x98, 0xdb), (1, 0x3d, 0x93), (1, 0x44, 0xb2),
    (1, 0x82, 0x00), (1, 0xcf, 0xdb), (1, 0x92, 0x93), (1, 0xb2, 0xb2), (1, 0xed, 0x00), (1, 0xcf, 0xdb), (1, 0x98, 0x93), (1, 0xac, 0xb2),
    (1, 0xaa, 0x00), (1, 0x37, 0xdb), (1, 0x6b, 0x93), (1, 0x76, 0xb2), (1, 0x67, 0x00), (1, 0x1d, 0xdb), (1, 0x2e, 0x93), (1, 0x89, 0xb2),
    (1, 0xf4, 0x00), (1, 0x14, 0xdb), (1, 0x8e, 0x93), (1, 0xe9, 0xb2), (1, 0xf0, 0x00), (1, 0x69, 0xdb), (1, 0x6e, 0x93), (1, 0xfe, 0xb2),
    (1, 0xb3, 0x00), (1, 0xec, 0xdb), (1, 0x7d, 0x93), (1, 0x33, 0xb2), (1, 0x3d, 0x00), (1, 0x43, 0xdb), (1, 0x2b, 0x93), (1, 0x01, 0xb2),
)
FIRST_MIX = (
    (0, 0x00, 0x00), (1, 0x00, 0x00), (1, 0x00, 0xd5), (2, 0x00, 0x00), (2, 0x00, 0x89), (0, 0x00, 0xd0), (1, 0x00, 0x09), (1, 0x00, 0x79),
    (1, 0x00, 0x21), (2, 0x00, 0x48), (0, 0x00, 0x48), (1, 0x00, 0x2b), (1, 0x00, 0x4d), (1, 0x00, 0x39), (2, 0x00, 0x04), (0, 0x00, 0xe4),
)
FIRST_FINAL = (
    (2, 0x00, 0x00), (2, 0x96, 0x48), (2, 0xcd, 0x42), (2, 0xc6, 0x56), (2, 0x19, 0x24), (2, 0x4d, 0x93), (2, 0xdb, 0x26), (2, 0x83, 0xa5),
    (2, 0x43, 0x9c), (2, 0x13, 0x62), (2, 0xde, 0x02), (2, 0x0b, 0xa4), (2, 0x97, 0x1d), (2, 0xe2, 0xd5), (2, 0x8d, 0xe5), (2, 0xb1, 0x8a),
)

# (base, input XOR, output XOR): 9 rounds of 16, then 4x4 mix columns, then 16 final.
SECOND_ROUNDS = (
    (3, 0x00, 0x00), (3, 0x5b, 0x38), (3, 0x91, 0xd7), (3, 0x52, 0x05), (3, 0x00, 0xa4), (3, 0x5b, 0x28), (3, 0x91, 0x14), (3, 0x52, 0x99),
    (3, 0x00, 0xc9), (3, 0x5b, 0xcd), (3, 0x91, 0xa7), (3, 0x52, 0x57), (3, 0x00, 0x80), (3, 0x5b, 0x8c), (3, 0x91, 0xa4), (3, 0x52, 0xad),
    (3, 0x00, 0x61), (3, 0x5b, 0x38), (3, 0x91, 0x97), (3, 0x52, 0xa5), (3, 0x00, 0x2c), (3, 0x5b, 0xf9), (3, 0x91, 0x6a), (3, 0x52, 0xd5),
    (3, 0x00, 0x0c), (3, 0x5b, 0xdd), (3, 0x91, 0x24), (3, 0x52, 0x6b), (3, 0x00, 0x65), (3, 0x5b, 0xb8), (3, 0x91, 0x69), (3, 0x52, 0x2f),
    (3, 0x00, 0x2a), (3, 0x5b, 0x7a), (3, 0x91, 0x30), (3, 0x52, 0x0c), (3, 0x00, 0xef), (3, 0x5b, 0x6a), (3, 0x91, 0xb3), (3, 0x52, 0x30),
    (3, 0x00, 0x0a), (3, 0x5b, 0x5e), (3, 0x91, 0x7e), (3, 0x52, 0xb2), (3, 0x00, 0x86), (3, 0x5b, 0x0f), (3, 0x91, 0xfe), (3, 0x52, 0x74),
    (3, 0x00, 0xc1), (3, 0x5b, 0x11), (3, 0x91, 0x7c), (3, 0x52, 0x17), (3, 0x00, 0xc7), (3, 0x5b, 0x92), (3, 0x91, 0x26), (3, 0x52, 0xce),
    (3, 0x00, 0x24), (3, 0x5b, 0x25), (3, 0x91, 0xb1), (3, 0x52, 0x95), (3, 0x00, 0x4b), (3, 0x5b, 0xc3), (3, 0x91, 0xa6), (3, 0x52, 0x08),
    (3, 0x00, 0x22), (3, 0x5b, 0xa6), (3, 0x91, 0x87), (3, 0x52, 0x76), (3, 0x00, 0x0c), (3, 0x5b, 0xdd), (3, 0x91, 0x48), (3, 0x52, 0x51),
    (3, 0x00, 0xc1), (3, 0x5b, 0x11), (3, 0x91, 0x10), (3, 0x52, 0x2d), (3, 0x00, 0x63), (3, 0x5b, 0x3b), (3, 0x91, 0x5f), (3, 0x52, 0xcc),
    (3, 0x00, 0x63), (3, 0x5b, 0xdd), (3, 0x91, 0x42), (3, 0x52, 0x4f), (3, 0x00, 0x86), (3, 0x5b, 0xe9), (3, 0x91, 0xe3), (3, 0x52, 0xf7),
    (3, 0x00, 0xae), (3, 0x5b, 0x11), (3, 0x91, 0x1a), (3, 0x52, 0x33), (3, 0x00, 0x24), (3, 0x5b, 0xc3), (3, 0x91, 0xac), (3, 0x52, 0x16),
    (3, 0x00, 0x82), (3, 0x5b, 0x94), (3, 0x91, 0x03), (3, 0x52, 0xe0), (3, 0x00, 0xed), (3, 0x5b, 0x94), (3, 0x91, 0x09), (3, 0x52, 0xfe),
    (3, 0x00, 0xaa), (3, 0x5b, 0x6c), (3, 0x91, 0xfa), (3, 0x52, 0x24), (3, 0x00, 0x67), (3, 0x5b, 0x46), (3, 0x91, 0xbf), (3, 0x52, 0xdb),
    (3, 0x00, 0xf4), (3, 0x5b, 0x4f), (3, 0x91, 0x1f), (3, 0x52, 0xbb), (3, 0x00, 0xf0), (3, 0x5b, 0x32), (3, 0x91, 0xff), (3, 0x52, 0xac),
    (3, 0x00, 0xb3), (3, 0x5b, 0xb7), (3, 0x91, 0xec), (3, 0x52, 0x61), (3, 0x00, 0x3d), (3, 0x5b, 0x18), (3, 0x91, 0xba), (3, 0x52, 0x53),
    (4, 0x00, 0x00), (4, 0x48, 0xcd), (4, 0x42, 0x5c), (4, 0x56, 0x94), (4, 0x24, 0x19), (4, 0x93, 0x16), (4, 0x26, 0x4a), (4, 0xa5, 0xd1),
    (4, 0x9c, 0x43), (4, 0x62, 0x48), (4, 0x02, 0x4f), (4, 0xa4, 0x59), (4, 0x1d, 0x97), (4, 0xd5, 0xb9), (4, 0xe5, 0x1c), (4, 0x8a, 0xe3),
)
SECOND_MIX = (
    (3, 0x00, 0x00), (4, 0x00, 0x00), (5, 0x00, 0x00), (6, 0x00, 0x00), (6, 0x00, 0x01), (3, 0x00, 0x83), (4, 0x00, 0xd2), (5, 0x00, 0x77),
    (5, 0x00, 0x67), (6, 0x00, 0x1f), (3, 0x00, 0x8c), (4, 0x00, 0xb8), (4, 0x00, 0xff), (5, 0x00, 0x5e), (6, 0x00, 0x56), (3, 0x00, 0x04),
)
SECOND_FINAL = (
    (5, 0x00, 0x00), (5, 0x5b, 0xde), (5, 0x91, 0x2e), (5, 0x52, 0x66), (5, 0x00, 0xb0), (5, 0x5b, 0x0d), (5, 0x91, 0x24), (5, 0x52, 0xf8),
    (5, 0x00, 0xd2), (5, 0x5b, 0xfd), (5, 0x91, 0x7e), (5, 0x52, 0xa1), (5, 0x00, 0x5b), (5, 0x5b, 0x9e), (5, 0x91, 0x19), (5, 0x52, 0xf6),
)
