# installer for EmonCMS
# Copyright 2014-2020 Matthew Wall
# Distributed under the terms of the GNU Public License (GPLv3)

from weecfg.extension import ExtensionInstaller

def loader():
    return EmonCMSInstaller()

class EmonCMSInstaller(ExtensionInstaller):
    def __init__(self):
        super(EmonCMSInstaller, self).__init__(
            version="0.17",
            name='emoncms',
            description='Upload weather data to EmonCMS.',
            author="Matthew Wall",
            author_email="mwall@users.sourceforge.net",
            restful_services='user.emoncms.EmonCMS',
            config={
                'StdRESTful': {
                    'EmonCMS': {
                        'token': 'INSERT_TOKEN_HERE'}}},
            files=[('bin/user', ['bin/user/emoncms.py'])]
            )
