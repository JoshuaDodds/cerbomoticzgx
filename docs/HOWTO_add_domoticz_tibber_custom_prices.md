
add the following to tibber.html in `/usr/share/domoticz/www/templates` to have a 
link to the generated graph in domoticz dashboard. 

```
<head>
<style>

</style>
</head>
<body>
<iframe src="https://tibber-graphs.s3-eu-west-1.amazonaws.com/prices.png"
        style="position: absolute;
               height: 480px;
               width: 800px;
               top: 50%;
               left: 50%;
               transform: translate(-50%, -50%) scale(1.35);">
</iframe>
</body>

```
