emoncms - weewx extension that sends data to EmonCMS
Copyright 2014 Matthew Wall

Installation instructions:

1) run the installer:

wee_extension --install weewx-emoncms.tgz

2) enter the EmonCMS token in weewx.conf:

[StdRESTful]
    [[EmonCMS]]
        token = TOKEN

3) restart weewx:

sudo /etc/init.d/weewx stop
sudo /etc/init.d/weewx start

The default installation will upload every observation in each archive record.
See comments in emoncms.py for options, including how to upload a subset of
data or to change the units or labels during upload.
